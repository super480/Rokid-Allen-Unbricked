"""P2-12 Mac 通知播报 · Host 侧过滤、去重、限时排队（纯逻辑）。

Mac 读取器（`app/notify/probe/` 验证中的 AX 方案）只把**已允许来源**的可见
字段送进来；本模块再按机主设置做一次过滤，决定是否排队、怎么出卡片/播报文案。
这里不做任何 I/O：不发声、不 emit、不写对话历史——接线留给 server/engine，
保证通知是单向输出，不模拟用户语音、不切换助手（任务书「接入若琪」一节）。

设计要点（对应任务书「事件和投递规则」）：
- 来源先认稳定标识（bundle id），只有显示名时查唯一映射，歧义保持静默；
- 去重优先稳定通知 ID；没有就用来源+可见字段摘要+短时间窗，且同一横幅
  消失后再出现视为真实第二条提醒；
- 锁屏或锁屏状态未知默认静默；暂停、来源开关、锁屏在出队时重查；
- 忙（对话/音乐）时延后，超过 TTL 丢弃并计数，失败不自动重播；
- 首次全部关闭；计数只记数字与原因，不留标题/正文。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import re
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

MODES = ("off", "card", "source", "title", "title_body")

# 默认展示的内置来源：仅保留「提醒事项」和「日历」，其余由用户在控制台自行添加。
BUILTIN_SOURCES: dict[str, str] = {
    "com.apple.reminders": "提醒事项",
    "com.apple.iCal": "日历",
}

# 常见 App 显示名 → 标准 bundle id 的辅助映射（用于解析与自定义添加时的 ID 归一化，
# 不会自动出现在控制台默认列表中）。
KNOWN_SOURCES: dict[str, str] = {
    **BUILTIN_SOURCES,
    "com.anthropic.claudefordesktop": "Claude",
    "com.openai.codex": "ChatGPT",
    "com.openai.chat": "ChatGPT",
    "com.tencent.workbuddy.mac": "WorkBuddy",
    "com.workbuddy.workbuddy-ai": "WorkBuddy AI",
    "ai.opencode.desktop": "OpenCode",
    "ai.nuphos.desktop": "Nuphos",
    "com.apple.ScriptEditor2": "脚本编辑器",
}

# AX 横幅里只能拿到显示名时的内置映射（中英系统语言都可能出现）。
_NAME_ALIASES: dict[str, str] = {
    "提醒事项": "com.apple.reminders",
    "Reminders": "com.apple.reminders",
    "日历": "com.apple.iCal",
    "Calendar": "com.apple.iCal",
    "Claude": "com.anthropic.claudefordesktop",
    "ChatGPT": "com.openai.codex",
    "Codex": "com.openai.codex",
    "WorkBuddy": "com.tencent.workbuddy.mac",
    "WorkBuddy AI": "com.workbuddy.workbuddy-ai",
    "OpenCode": "ai.opencode.desktop",
    "Nuphos": "ai.nuphos.desktop",
    "脚本编辑器": "com.apple.ScriptEditor2",
    "Script Editor": "com.apple.ScriptEditor2",
}

TITLE_MAX = 80
BODY_MAX = 200

DEFAULT_SETTINGS: dict = {
    "enabled": False,          # 首次全部关闭
    "sources": {},             # source_id → mode
    "custom_sources": [],      # 用户自定义添加的 App：[{"id": ..., "name": ...}]
    "paused_until": 0.0,       # epoch 秒；控制台「暂停一小时」
    "ttl_seconds": 30.0,       # 忙时延后的最长等待
    "dedupe_window": 20.0,     # 无稳定 ID 时的去重时间窗
    "redraw_dedupe_window": 5.0,  # 拦截系统 1~2 秒内换 UUID 重绘同内容
}


@dataclass
class NotifyEvent:
    source_id: str
    source_name: str
    title: str = ""
    body: str = ""
    subtitle: str = ""
    event_id: str = ""
    received_at: float = 0.0

    @property
    def content_key(self) -> str:
        digest = hashlib.sha256(
            "\x1f".join((self.title, self.subtitle, self.body)).encode("utf-8")
        ).hexdigest()[:16]
        return f"sum:{self.source_id}:{digest}"

    @property
    def dedupe_key(self) -> str:
        if self.event_id:
            return f"id:{self.source_id}:{self.event_id}"
        return self.content_key


@dataclass
class Decision:
    action: str            # queued / dropped
    reason: str = ""


@dataclass
class Delivery:
    event: NotifyEvent
    mode: str
    card: dict
    speech: Optional[str]


@dataclass
class _Queued:
    event: NotifyEvent
    queued_at: float


def _clip(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def resolve_source(
    source_id: str = "",
    source_name: str = "",
    custom_aliases: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """优先稳定标识；只有显示名时先查用户自定义映射再查内置映射，歧义返回 None（静默）。"""
    if isinstance(source_id, str) and source_id.strip():
        return source_id.strip()
    if isinstance(source_name, str):
        key = source_name.strip()
        if custom_aliases and key in custom_aliases:
            return custom_aliases[key]
        return _NAME_ALIASES.get(key)
    return None


def normalize_event(
    raw: dict,
    now: float,
    custom_aliases: Optional[dict[str, str]] = None,
    custom_names: Optional[dict[str, str]] = None,
) -> Optional[NotifyEvent]:
    if not isinstance(raw, dict):
        return None
    sid = resolve_source(
        raw.get("source_id", ""),
        raw.get("source_name", ""),
        custom_aliases=custom_aliases,
    )
    if not sid:
        return None
    display_map = dict(KNOWN_SOURCES)
    if custom_names:
        display_map.update(custom_names)
    name = _clip(raw.get("source_name"), 24) or display_map.get(sid, "")
    if not name:
        return None
    ev = NotifyEvent(
        source_id=sid,
        source_name=display_map.get(sid, name),
        title=_clip(raw.get("title"), TITLE_MAX),
        subtitle=_clip(raw.get("subtitle"), TITLE_MAX),
        body=_clip(raw.get("body"), BODY_MAX),
        event_id=_clip(raw.get("event_id"), 128),
        received_at=now,
    )
    if not (ev.title or ev.body):
        return None
    return ev


def compose(ev: NotifyEvent, mode: str) -> tuple[dict, Optional[str]]:
    """卡片字段与播报文案。只用横幅上实际可见的文字，不做推断。"""
    card = {"source": ev.source_name, "title": ev.title}
    if mode == "title_body" and ev.body:
        card["body"] = ev.body
    if mode == "card" or mode == "off":
        return card, None
    if mode == "source":
        return card, ev.source_name
    headline = ev.title or ev.body
    speech = f"{ev.source_name}，{headline}"
    if mode == "title_body" and ev.title and ev.body:
        speech += f"。{ev.body}"
    return card, speech


class NotifyRelay:
    def __init__(
        self,
        settings: Optional[dict] = None,
        clock: Callable[[], float] = time.time,
        is_locked: Callable[[], Optional[bool]] = lambda: None,
        max_queue: int = 8,
    ):
        self.settings = dict(DEFAULT_SETTINGS)
        self.update_settings(settings or {})
        self.clock = clock
        self.is_locked = is_locked
        self.queue: deque[_Queued] = deque(maxlen=max_queue)
        self._seen: dict[str, float] = {}       # dedupe_key → 最近收到时间
        self._seen_content: dict[str, float] = {}  # content_key → 最近入队时间（防换 UUID 重绘）
        self._removed: set[str] = set()         # 已观察到横幅消失的 key
        self.counters: dict[str, int] = {}

    # -- 设置 --------------------------------------------------------------
    def update_settings(self, patch: dict) -> dict:
        for key in ("enabled", "paused_until", "ttl_seconds", "dedupe_window", "redraw_dedupe_window"):
            if key in patch:
                self.settings[key] = patch[key]
        if isinstance(patch.get("sources"), dict):
            sources = dict(self.settings.get("sources") or {})
            for sid, mode in patch["sources"].items():
                if isinstance(sid, str) and mode in MODES:
                    sources[sid] = mode
            self.settings["sources"] = sources
        if isinstance(patch.get("custom_sources"), list):
            cleaned = []
            seen_ids = set(BUILTIN_SOURCES.keys())
            for item in patch["custom_sources"]:
                if not isinstance(item, dict):
                    continue
                sid = str(item.get("id") or "").strip()
                name = _clip(item.get("name"), 32)
                if sid and name and sid not in seen_ids:
                    seen_ids.add(sid)
                    entry = {"id": sid, "name": name}
                    raw_aliases = item.get("aliases")
                    if isinstance(raw_aliases, list):
                        aliases = [
                            a for a in (_clip(x, 32) for x in raw_aliases)
                            if a
                        ]
                        if aliases:
                            entry["aliases"] = list(dict.fromkeys(aliases))
                    cleaned.append(entry)
            self.settings["custom_sources"] = cleaned
        return self.settings

    def custom_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        """返回 (name→id 映射, id→name 映射)，供 AX 显示名解析使用。"""
        aliases: dict[str, str] = {}
        names: dict[str, str] = {}
        for item in self.settings.get("custom_sources") or []:
            sid = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            if sid and name:
                aliases[name] = sid
                names[sid] = name
                for a in item.get("aliases") or []:
                    if isinstance(a, str) and a.strip():
                        aliases[a.strip()] = sid
        return aliases, names

    def pause(self, seconds: float = 3600) -> None:
        self.settings["paused_until"] = self.clock() + seconds

    def mode_for(self, source_id: str) -> str:
        return (self.settings.get("sources") or {}).get(source_id, "off")

    # -- 计数 --------------------------------------------------------------
    def _count(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    def _drop(self, reason: str) -> Decision:
        self._count(f"dropped:{reason}")
        return Decision("dropped", reason)

    def _gate(self, source_id: str) -> Optional[str]:
        """返回静默原因；None 表示可以继续。"""
        if not self.settings.get("enabled"):
            return "disabled"
        if self.mode_for(source_id) == "off":
            return "source_off"
        if self.clock() < float(self.settings.get("paused_until") or 0):
            return "paused"
        if self.is_locked() is not False:
            return "locked_or_unknown"
        return None

    # -- 入口 --------------------------------------------------------------
    def submit(self, raw: dict) -> Decision:
        now = self.clock()
        self._count("received")
        aliases, names = self.custom_maps()
        ev = normalize_event(raw, now, custom_aliases=aliases, custom_names=names)
        if ev is None:
            return self._drop("invalid_or_ambiguous")
        reason = self._gate(ev.source_id)
        if reason:
            return self._drop(reason)
        key = ev.dedupe_key
        ckey = ev.content_key
        last = self._seen.get(key)
        last_content = self._seen_content.get(ckey)
        window = float(self.settings.get("dedupe_window") or 0)
        redraw_window = float(self.settings.get("redraw_dedupe_window") or 0)
        if ev.event_id and last is not None and key not in self._removed:
            return self._drop("duplicate")
        if not ev.event_id and last is not None and now - last < window and key not in self._removed:
            return self._drop("duplicate")
        # 拦截 macOS 通知中心短时间内销毁旧 UUID 并用新 UUID 重绘完全相同内容
        if redraw_window > 0 and last_content is not None and now - last_content < redraw_window:
            return self._drop("duplicate")
        self._seen[key] = now
        self._seen_content[ckey] = now
        self._removed.discard(key)
        self._prune(now)
        if len(self.queue) == self.queue.maxlen:
            self._count("dropped:queue_full")
        self.queue.append(_Queued(ev, now))
        self._count("queued")
        return Decision("queued")

    def banner_removed(self, raw: dict) -> None:
        """读取器报告横幅消失：之后同一内容再出现算真实的第二条提醒。"""
        aliases, names = self.custom_maps()
        ev = normalize_event(raw, self.clock(), custom_aliases=aliases, custom_names=names)
        if ev is not None:
            self._removed.add(ev.dedupe_key)

    def pop_ready(self, busy: bool = False) -> Optional[Delivery]:
        """出队一条可投递的通知；下发前重查开关、暂停、锁屏与 TTL。"""
        now = self.clock()
        ttl = float(self.settings.get("ttl_seconds") or 0)
        while self.queue:
            item = self.queue[0]
            if ttl and now - item.queued_at > ttl:
                self.queue.popleft()
                self._count("dropped:expired")
                continue
            reason = self._gate(item.event.source_id)
            if reason:
                self.queue.popleft()
                self._count(f"dropped:{reason}")
                continue
            if busy:
                self._count("deferred")
                return None
            self.queue.popleft()
            mode = self.mode_for(item.event.source_id)
            card, speech = compose(item.event, mode)
            self._count("delivered")
            return Delivery(item.event, mode, card, speech)
        return None

    def _prune(self, now: float) -> None:
        horizon = max(float(self.settings.get("dedupe_window") or 0), 600.0)
        for key in [k for k, t in self._seen.items() if now - t > horizon]:
            self._seen.pop(key, None)
            self._removed.discard(key)
        for key in [k for k, t in self._seen_content.items() if now - t > horizon]:
            self._seen_content.pop(key, None)

    def status(self) -> dict:
        return {
            "enabled": bool(self.settings.get("enabled")),
            "paused_until": self.settings.get("paused_until") or 0,
            "queued": len(self.queue),
            "counters": dict(self.counters),
        }


# ---------------------------------------------------------------------------
# Unix domain socket 接收器（仅当前用户私有目录，权限 0700 / 0600）
# ---------------------------------------------------------------------------

def default_socket_path() -> Path:
    base = Path.home() / "Library" / "Application Support" / "Rokid-Allen-Unbricked"
    candidate = base / "notify.sock"
    # macOS AF_UNIX sun_path 上限为 104 字节；若 HOME 过长则回退到用户私有临时目录（0700）。
    if len(str(candidate).encode("utf-8")) <= 96:
        return candidate
    return Path(tempfile.gettempdir()) / f"rokid-notify-{os.getuid()}" / "notify.sock"


class NotifySocketServer:
    """本机私有 Unix stream socket：逐行接收读取器发来的 JSONL 事件。"""

    def __init__(self, socket_path: Path, on_message: Callable[[dict], None]):
        self.socket_path = Path(socket_path)
        self.on_message = on_message
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> Path:
        if self._thread and self._thread.is_alive():
            return self.socket_path
        parent = self.socket_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.socket_path))
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError:
            pass
        sock.listen(8)
        sock.settimeout(0.5)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, name="notify-socket", daemon=True
        )
        self._thread.start()
        return self.socket_path

    def _serve(self) -> None:
        sock = self._sock
        while not self._stop.is_set() and sock is not None:
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_conn, args=(conn,),
                name="notify-conn", daemon=True,
            ).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5.0)
            with conn.makefile("r", encoding="utf-8", errors="replace") as reader:
                for line in reader:
                    if self._stop.is_set():
                        break
                    raw = line.strip()
                    if not raw or len(raw) > 16384:
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(msg, dict):
                        try:
                            self.on_message(msg)
                        except Exception:
                            pass
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 本机应用程序目录扫描（仅读取 .app 的 Info.plist 与本地化显示名）
# ---------------------------------------------------------------------------

_STRINGS_KV_RE = re.compile(r'"(CFBundleDisplayName|CFBundleName)"\s*=\s*"([^"]+)"')


def _read_localized_name(res_dir: Path) -> str:
    """优先从 InfoPlist.loctable 或 zh_CN.lproj/InfoPlist.strings 提取中文显示名。"""
    loctable = res_dir / "InfoPlist.loctable"
    if loctable.is_file():
        try:
            data = plistlib.loads(loctable.read_bytes())
            for loc in ("zh_CN", "zh-Hans", "zh_Hans"):
                sec = data.get(loc)
                if isinstance(sec, dict):
                    val = str(sec.get("CFBundleDisplayName") or sec.get("CFBundleName") or "").strip()
                    if val:
                        return val
        except Exception:
            pass
    for loc in ("zh_CN.lproj", "zh-Hans.lproj", "zh_Hans.lproj"):
        lp = res_dir / loc / "InfoPlist.strings"
        if not lp.is_file():
            continue
        raw = lp.read_bytes()
        try:
            ld = plistlib.loads(raw)
            if isinstance(ld, dict):
                val = str(ld.get("CFBundleDisplayName") or ld.get("CFBundleName") or "").strip()
                if val:
                    return val
        except Exception:
            pass
        # 兼容旧版 UTF-16 / UTF-8 文本格式 .strings
        for enc in ("utf-16", "utf-8"):
            try:
                text = raw.decode(enc)
            except UnicodeDecodeError:
                continue
            found = dict(_STRINGS_KV_RE.findall(text))
            val = (found.get("CFBundleDisplayName") or found.get("CFBundleName") or "").strip()
            if val:
                return val
    return ""


def default_app_dirs() -> list[Path]:
    return [
        Path("/Applications"),
        Path("/System/Applications"),
        Path.home() / "Applications",
    ]


def scan_installed_apps(app_dirs: Optional[list[Path]] = None) -> list[dict]:
    """扫描本机应用程序目录，返回 [{id, name, aliases, path}]（按显示名排序）。"""
    dirs = app_dirs if app_dirs is not None else default_app_dirs()
    by_id: dict[str, dict] = {}
    for d in dirs:
        p = Path(d)
        if not p.is_dir():
            continue
        try:
            entries = sorted(p.glob("*.app"))
        except OSError:
            continue
        for app in entries:
            ip = app / "Contents" / "Info.plist"
            if not ip.is_file():
                continue
            try:
                info = plistlib.loads(ip.read_bytes())
            except Exception:
                continue
            if not isinstance(info, dict):
                continue
            bid = str(info.get("CFBundleIdentifier") or "").strip()
            if not bid or bid in by_id:
                continue
            en_disp = str(info.get("CFBundleDisplayName") or "").strip()
            en_name = str(info.get("CFBundleName") or "").strip()
            zh_name = _read_localized_name(app / "Contents" / "Resources")
            display = zh_name or en_disp or en_name or app.stem
            aliases = list(dict.fromkeys(
                a for a in (zh_name, en_disp, en_name, app.stem)
                if a
            ))
            by_id[bid] = {
                "id": bid,
                "name": _clip(display, 32),
                "aliases": aliases,
                "path": str(app),
            }
    return sorted(by_id.values(), key=lambda x: (x["name"].lower(), x["id"]))


def _safe_icon_filename(source_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", source_id).strip("._")
    return (cleaned or "app") + ".png"


def default_icon_cache_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "runtime" / "notify-icons"


def extract_app_icon_png(app_path: str, out_path: Path) -> bool:
    """调用 Swift 工具把 Mac .app 图标导出为 128x128 透明 PNG（支持 Assets.car）。"""
    cli = Path(__file__).resolve().parent.parent / "build" / "notify-probe" / "notify-probe-cli"
    if not cli.is_file() or not app_path:
        return False
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            [str(cli), "extract-icon", "--app", str(app_path), "--out", str(out_path)],
            capture_output=True, text=True, timeout=8,
        )
        return res.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Host 侧服务封装（供 engine.py / server.py / console.html 接线）
# ---------------------------------------------------------------------------

READER_STALE_SECONDS = 30.0
PREVIEW_TTL_SECONDS = 60.0
TEST_CARD_TTL_MS = 8000
NOTIFY_CARD_TTL_MS = 10000
INFLIGHT_TIMEOUT_SECONDS = 25.0
DEFER_RETRY_SECONDS = 1.5


class NotifyService:
    """Mac 通知播报 Host 服务。

    - 首次全部关闭，所有设置经 `save` 回调落盘；
    - 仅记录计数与状态，不把真实通知标题/正文写入配置或日志；
    - 「预览下一条」仅在内存中短暂保留一条预览，到期自动清除，不下发设备；
    - 「静音测试卡」只发 `miniapp_card` 卡片，不调用 TTS、不写对话历史、不切助手。
    """

    def __init__(
        self,
        engine=None,
        config: Optional[dict] = None,
        save: Optional[Callable[[], None]] = None,
        clock: Callable[[], float] = time.time,
        app_dirs: Optional[list[Path]] = None,
        icon_cache_dir: Optional[Path] = None,
    ):
        self.engine = engine
        self._save = save
        self._clock = clock
        self._app_dirs = app_dirs
        self._icon_cache_dir = Path(icon_cache_dir) if icon_cache_dir else default_icon_cache_dir()
        self._lock = threading.RLock()
        self._reader: dict = {
            "connected": False,
            "trusted": None,       # None=未知 / True=已授权 / False=未授权
            "locked": None,        # None=未知 / True=锁屏 / False=解锁
            "last_seen": 0.0,
            "pid": None,
            "macOS": "",
        }
        self._preview_armed_until: float = 0.0
        self._preview_source_id: str = ""
        self._preview_item: Optional[dict] = None
        self._preview_expires_at: float = 0.0
        self._socket_server: Optional[NotifySocketServer] = None
        self._card_gen: int = 0
        self._inflight_request_id: str = ""
        self._inflight_until: float = 0.0
        self._retry_timer: Optional[threading.Timer] = None
        self._closed: bool = False

        self.relay = NotifyRelay(
            settings=config or {},
            clock=clock,
            is_locked=self._is_locked,
        )

    def _is_locked(self) -> Optional[bool]:
        with self._lock:
            if not self._reader_alive_locked():
                return None
            return self._reader["locked"]

    def _reader_alive_locked(self) -> bool:
        last = float(self._reader.get("last_seen") or 0.0)
        return last > 0 and (self._clock() - last) <= READER_STALE_SECONDS

    def _persist(self) -> None:
        if self.engine is not None and isinstance(getattr(self.engine, "config", None), dict):
            self.engine.config["notify"] = {
                "enabled": bool(self.relay.settings.get("enabled")),
                "sources": dict(self.relay.settings.get("sources") or {}),
                "custom_sources": list(self.relay.settings.get("custom_sources") or []),
                "paused_until": float(self.relay.settings.get("paused_until") or 0.0),
                "ttl_seconds": float(self.relay.settings.get("ttl_seconds") or 30.0),
                "dedupe_window": float(self.relay.settings.get("dedupe_window") or 20.0),
            }
        if callable(self._save):
            self._save()

    # -- Unix socket 生命周期 -----------------------------------------------
    def start_socket(self, socket_path: Optional[Path] = None) -> Path:
        with self._lock:
            if self._socket_server is not None:
                return self._socket_server.socket_path
            path = Path(socket_path) if socket_path else default_socket_path()
            srv = NotifySocketServer(path, self.ingest_message)
            srv.start()
            self._socket_server = srv
            return path

    def close(self) -> None:
        with self._lock:
            self._closed = True
            srv = self._socket_server
            self._socket_server = None
            timer = self._retry_timer
            self._retry_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        if srv is not None:
            srv.close()

    # -- 读取器入站 ---------------------------------------------------------
    def ingest_message(self, msg: dict) -> dict:
        """处理来自 Mac 读取器（Unix socket）的单条 JSON 消息。"""
        if not isinstance(msg, dict):
            return {"ok": False, "reason": "bad_message"}
        kind = str(msg.get("type") or "")
        now = self._clock()
        with self._lock:
            self._reader["last_seen"] = now
            self._reader["connected"] = True
            if "trusted" in msg and isinstance(msg["trusted"], bool):
                self._reader["trusted"] = msg["trusted"]
            if "locked" in msg and isinstance(msg["locked"], bool):
                self._reader["locked"] = msg["locked"]
            if "pid" in msg:
                self._reader["pid"] = msg.get("pid")
            if isinstance(msg.get("macOS"), str):
                self._reader["macOS"] = msg["macOS"]

            if kind in ("heartbeat", "start", "check", "attach", "trust_granted", "trust_lost"):
                if kind == "trust_lost":
                    self._reader["trusted"] = False
                elif kind == "trust_granted":
                    self._reader["trusted"] = True
                return {"ok": True, "type": kind}

            if kind == "banner_removed":
                self.relay.banner_removed(msg)
                return {"ok": True, "type": kind}

            if kind == "banner_added":
                aliases, names = self.relay.custom_maps()
                ev = normalize_event(msg, now, custom_aliases=aliases, custom_names=names)
                # 预览模式：仅在本机短暂展示机主选定的测试来源，不下发设备。
                if now < self._preview_armed_until and ev is not None:
                    if not self._preview_source_id or ev.source_id == self._preview_source_id:
                        self._preview_item = {
                            "source_id": ev.source_id,
                            "source_name": ev.source_name,
                            "title": ev.title,
                            "subtitle": ev.subtitle,
                            "body": ev.body,
                            "captured_at": now,
                        }
                        self._preview_expires_at = now + PREVIEW_TTL_SECONDS
                        self._preview_armed_until = 0.0
                        self.relay._count("preview_captured")
                        return {"ok": True, "action": "preview_captured"}
                dec = self.relay.submit(msg)
                should_flush = dec.action == "queued" and self.engine is not None
        if kind == "banner_added" and should_flush:
            self.flush_ready()
        if kind == "banner_added":
            return {"ok": True, "action": dec.action, "reason": dec.reason}

        return {"ok": False, "reason": "unknown_type"}

    # -- 批次 4：单向通知卡片与播报消费队列 ---------------------------------
    def _is_busy_locked(self, now: float) -> bool:
        if self._inflight_request_id and now < self._inflight_until:
            return True
        if self._inflight_request_id and now >= self._inflight_until:
            # 回执超时：视为状态不明，清除在途标记但不自动重播。
            self._inflight_request_id = ""
            self._inflight_until = 0.0
            self.relay._count("playback_timeout")
        eng = self.engine
        if eng is None:
            return False
        checker = getattr(eng, "is_busy_for_notify", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return True
        return bool(getattr(eng, "busy", False))

    def _schedule_retry_locked(self) -> None:
        if self._closed or self._retry_timer is not None or not self.relay.queue:
            return
        timer = threading.Timer(DEFER_RETRY_SECONDS, self._on_retry_timer)
        timer.daemon = True
        self._retry_timer = timer
        timer.start()

    def _on_retry_timer(self) -> None:
        with self._lock:
            self._retry_timer = None
        if not self._closed:
            self.flush_ready()

    def flush_ready(self) -> Optional[dict]:
        """出队并向若琪下发一条单向通知（仅在绑定了 engine 时执行）。

        隔离保证：
        - 绝不调用 engine.deliver_speech（避免被当前选中助手如猫娘改为 Mac 发声或切换头像）；
        - 绝不写对话历史、绝不切换当前助手、绝不模拟用户语音输入；
        - 失败或回执不明绝不自动重播。
        """
        eng = self.engine
        if eng is None or not callable(getattr(eng, "emit", None)):
            return None
        now = self._clock()
        with self._lock:
            busy = self._is_busy_locked(now)
            delivery = self.relay.pop_ready(busy=busy)
            if delivery is None:
                if busy and self.relay.queue:
                    self._schedule_retry_locked()
                return None
            self._card_gen = max(self._card_gen + 1, int(now * 1000))
            card_id = f"notify-{uuid.uuid4().hex[:8]}"
            request_id = f"notify-{uuid.uuid4().hex[:8]}"
            expires_ms = int(now * 1000) + NOTIFY_CARD_TTL_MS
            card_payload = {
                "action": "show",
                "app_id": "mac_notify",
                "operation": "notify",
                "card_id": card_id,
                "generation": self._card_gen,
                "server_instance_id": getattr(eng, "instance_id", ""),
                "request_id": request_id,
                "ttl_ms": NOTIFY_CARD_TTL_MS,
                "expires_at_ms": expires_ms,
                "card": self._build_carousel_card(
                    delivery.event.source_id,
                    delivery.event.source_name,
                    title=delivery.event.title,
                    body=delivery.card.get("body", ""),
                ),
            }
            speak_payload = None
            if delivery.speech:
                speak_payload = {
                    "text": delivery.speech,
                    "request_id": request_id,
                    "agent": "notify",
                    "card_id": card_id,
                    "expires_at_ms": expires_ms,
                }
                self._inflight_request_id = request_id
                self._inflight_until = now + INFLIGHT_TIMEOUT_SECONDS
            self.relay._count("dispatched_card")
            if speak_payload is not None:
                self.relay._count("dispatched_speech")

        # 锁外下发，避免与 engine.lock 互锁。
        eng.emit("miniapp_card", **card_payload)
        if speak_payload is not None:
            eng.emit("speak", **speak_payload)
        return {
            "card_id": card_id,
            "request_id": request_id,
            "mode": delivery.mode,
            "spoken": speak_payload is not None,
        }

    def on_device_event(self, kind: str, event: Optional[dict] = None) -> None:
        """接收设备端回执（playback_done / playback_error / tts_error / playback_stopped）。"""
        ev = event if isinstance(event, dict) else {}
        req_id = str(ev.get("request_id") or "")
        should_flush = False
        with self._lock:
            is_ours = bool(self._inflight_request_id) and (
                not req_id or req_id == self._inflight_request_id or req_id.isdigit()
            )
            if is_ours:
                self._inflight_request_id = ""
                self._inflight_until = 0.0
                if kind == "playback_done":
                    self.relay._count("playback_done")
                else:
                    # 失败/中止不自动重播，仅分开计数。
                    self.relay._count("playback_error")
            if kind in ("playback_done", "playback_stopped", "playback_error", "tts_error"):
                should_flush = bool(self.relay.queue)
        if should_flush and not self._closed:
            self.flush_ready()

    # -- 控制台操作 ---------------------------------------------------------
    def update_settings(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            return self.status()
        with self._lock:
            self.relay.update_settings(patch)
            self._persist()
            return self.status()

    def _icon_path(self, source_id: str) -> Path:
        return self._icon_cache_dir / _safe_icon_filename(source_id)

    def _read_icon_b64(self, source_id: str) -> str:
        p = self._icon_path(source_id)
        if not p.is_file():
            # 对旧配置中已存在但尚无缓存的自定义 App 自动补提一次图标
            for app in scan_installed_apps(self._app_dirs):
                if app["id"] == source_id and app.get("path"):
                    extract_app_icon_png(app["path"], p)
                    break
        if not p.is_file():
            return ""
        try:
            raw = p.read_bytes()
            if 0 < len(raw) <= 65536:
                return base64.b64encode(raw).decode("ascii")
        except OSError:
            pass
        return ""

    def _build_carousel_card(
        self,
        source_id: str,
        source_name: str,
        title: str = "",
        body: str = "",
    ) -> dict:
        """构造 design/mac-notification-carousel/index.html 1:1 对应的滚动卡片。

        规则（按机主明确要求）：
        - 无论有没有第三个 App，前两个槽位永远是「日历」和「提醒事项」；
        - 播报哪个 App 的通知，就把那个 App 停在中间选框（target_index）；
        - 自定义 App 附带已提取保存的图标 base64。
        """
        items: list[dict] = [
            {"id": "com.apple.iCal", "name": "日历", "asset": "notify_calendar.png"},
            {"id": "com.apple.reminders", "name": "提醒事项", "asset": "notify_reminders.png"},
        ]
        if source_id == "com.apple.iCal":
            target_index = 0
        elif source_id == "com.apple.reminders":
            target_index = 1
        else:
            entry: dict = {"id": source_id, "name": source_name}
            b64 = self._read_icon_b64(source_id)
            if b64:
                entry["icon_b64"] = b64
            items.append(entry)
            target_index = 2
        # 若当前播报的是日历/提醒事项，且用户已有自定义 App，也把第一个自定义 App 放入第 3 槽参与视觉轮播。
        if len(items) == 2:
            custom_list = list(self.relay.settings.get("custom_sources") or [])
            if custom_list:
                first = custom_list[0]
                cid = str(first.get("id") or "")
                cname = str(first.get("name") or "")
                if cid and cname:
                    entry = {"id": cid, "name": cname}
                    b64 = self._read_icon_b64(cid)
                    if b64:
                        entry["icon_b64"] = b64
                    items.append(entry)
        return {
            "kind": "mac_notify",
            "layout": "notify_carousel",
            "title": source_name,
            "subtitle": title or body,
            "target_index": target_index,
            "items": items,
        }

    def installed_apps(self) -> list[dict]:
        """返回本机已安装且尚未加入列表的 App，供控制台下拉选取。"""
        apps = scan_installed_apps(self._app_dirs)
        with self._lock:
            existing = set(BUILTIN_SOURCES.keys()) | {
                str(item.get("id") or "")
                for item in (self.relay.settings.get("custom_sources") or [])
            }
        return [
            {"id": a["id"], "name": a["name"], "aliases": a["aliases"]}
            for a in apps if a["id"] not in existing
        ]

    def add_custom_source(
        self, source_id: str = "", mode: str = "title"
    ) -> dict:
        """从本机已安装应用中选取并添加，同时提取并缓存 App 图标供特效使用。"""
        sid = str(source_id or "").strip()
        if not sid:
            return {"ok": False, "error": "请先从列表中选择一个本机应用"}
        if sid in BUILTIN_SOURCES:
            return {"ok": False, "error": f"「{BUILTIN_SOURCES[sid]}」已是内置应用，无需重复添加"}
        chosen_mode = mode if mode in MODES else "title"
        installed = {a["id"]: a for a in scan_installed_apps(self._app_dirs)}
        app = installed.get(sid)
        if app is None:
            return {"ok": False, "error": "未在本机应用程序目录找到该应用，请刷新后重新选择"}
        icon_ok = extract_app_icon_png(app.get("path", ""), self._icon_path(sid))
        with self._lock:
            custom = list(self.relay.settings.get("custom_sources") or [])
            if any(item.get("id") == sid for item in custom):
                return {"ok": False, "error": f"「{app['name']}」已在列表中"}
            custom.append({
                "id": sid,
                "name": app["name"],
                "aliases": app["aliases"],
                "has_icon": icon_ok,
            })
            self.relay.update_settings({
                "custom_sources": custom,
                "sources": {sid: chosen_mode},
            })
            self._persist()
            st = self.status()
            st["ok"] = True
            st["icon_saved"] = icon_ok
            st["message"] = f"已添加「{app['name']}」" + ("（图标已保存）" if icon_ok else "")
            return st

    def remove_custom_source(self, source_id: str) -> dict:
        """删除用户自定义添加的 App 并清理图标缓存（内置提醒事项/日历不可删除）。"""
        sid = str(source_id or "").strip()
        if not sid:
            return {"ok": False, "error": "缺少 source_id"}
        if sid in BUILTIN_SOURCES:
            return {"ok": False, "error": "内置应用不可删除，可将其模式设为关闭"}
        with self._lock:
            custom = [
                item for item in (self.relay.settings.get("custom_sources") or [])
                if item.get("id") != sid
            ]
            sources = dict(self.relay.settings.get("sources") or {})
            sources.pop(sid, None)
            self.relay.settings["custom_sources"] = custom
            self.relay.settings["sources"] = sources
            self._persist()
            try:
                self._icon_path(sid).unlink(missing_ok=True)
            except OSError:
                pass
            st = self.status()
            st["ok"] = True
            return st

    def pause(self, seconds: float = 3600.0) -> dict:
        with self._lock:
            if seconds <= 0:
                self.relay.settings["paused_until"] = 0.0
            else:
                self.relay.pause(float(seconds))
            self._persist()
            return self.status()

    def arm_preview(self, source_id: str = "", ttl: float = PREVIEW_TTL_SECONDS) -> dict:
        now = self._clock()
        with self._lock:
            if ttl <= 0:
                self._preview_armed_until = 0.0
                self._preview_item = None
                self._preview_expires_at = 0.0
            else:
                self._preview_source_id = str(source_id or "").strip()
                self._preview_armed_until = now + float(ttl)
                self._preview_item = None
                self._preview_expires_at = 0.0
            return self.status()

    def send_test_card(
        self,
        source_id: str = "com.apple.iCal",
        speak: bool = True,
        text: str = "一小时后，您在大会议室有个重要会议",
    ) -> dict:
        """发送一张测试卡到若琪屏幕；默认附带语音播报（单向输出，不切助手、不写历史）。"""
        sid = str(source_id or "com.apple.iCal").strip()
        source_name = KNOWN_SOURCES.get(sid, "日历")
        spoken_text = _clip(text, BODY_MAX) if speak else ""
        now = self._clock()
        with self._lock:
            self._card_gen = max(self._card_gen + 1, int(now * 1000))
            card_id = f"notify-{uuid.uuid4().hex[:8]}"
            request_id = f"notify-{uuid.uuid4().hex[:8]}"
            expires_ms = int(now * 1000) + TEST_CARD_TTL_MS
            card = self._build_carousel_card(sid, source_name, title=spoken_text or "测试通知")
            payload = {
                "action": "show",
                "app_id": "mac_notify",
                "operation": "test_card",
                "card_id": card_id,
                "generation": self._card_gen,
                "server_instance_id": getattr(self.engine, "instance_id", "") if self.engine else "",
                "request_id": request_id,
                "ttl_ms": TEST_CARD_TTL_MS,
                "expires_at_ms": expires_ms,
                "card": card,
            }
            speak_payload = None
            if spoken_text:
                speak_payload = {
                    "text": spoken_text,
                    "request_id": request_id,
                    "agent": "notify",
                    "card_id": card_id,
                    "expires_at_ms": expires_ms,
                }
                self._inflight_request_id = request_id
                self._inflight_until = now + INFLIGHT_TIMEOUT_SECONDS
            self.relay._count("test_card_sent")
        emitted = False
        if self.engine is not None and callable(getattr(self.engine, "emit", None)):
            self.engine.emit("miniapp_card", **payload)
            if speak_payload is not None:
                self.engine.emit("speak", **speak_payload)
            emitted = True
        return {
            "ok": True,
            "emitted": emitted,
            "spoken": bool(speak_payload),
            "text": spoken_text,
            "card_id": card_id,
            "card": payload["card"],
        }

    def status(self) -> dict:
        now = self._clock()
        with self._lock:
            alive = self._reader_alive_locked()
            if self._preview_item is not None and now > self._preview_expires_at:
                self._preview_item = None
                self._preview_expires_at = 0.0
            armed = now < self._preview_armed_until
            # 默认只展示内置的「提醒事项」「日历」+ 用户自己添加的自定义应用
            catalog = [
                {
                    "id": sid,
                    "name": name,
                    "mode": self.relay.mode_for(sid),
                    "builtin": True,
                }
                for sid, name in BUILTIN_SOURCES.items()
            ]
            for item in self.relay.settings.get("custom_sources") or []:
                sid = str(item.get("id") or "").strip()
                name = str(item.get("name") or "").strip()
                if sid and name and sid not in BUILTIN_SOURCES:
                    catalog.append({
                        "id": sid,
                        "name": name,
                        "mode": self.relay.mode_for(sid),
                        "builtin": False,
                    })

            paused_until = float(self.relay.settings.get("paused_until") or 0.0)
            return {
                "enabled": bool(self.relay.settings.get("enabled")),
                "paused": now < paused_until,
                "paused_until": paused_until,
                "paused_remaining_s": max(0, int(paused_until - now)) if now < paused_until else 0,
                "sources": dict(self.relay.settings.get("sources") or {}),
                "custom_sources": list(self.relay.settings.get("custom_sources") or []),
                "catalog": catalog,
                "modes": list(MODES),
                "queued": len(self.relay.queue),
                "counters": dict(self.relay.counters),
                "socket_path": str(self._socket_server.socket_path) if self._socket_server else "",
                "reader": {
                    "connected": alive,
                    "trusted": self._reader["trusted"] if alive else None,
                    "locked": self._reader["locked"] if alive else None,
                    "last_seen": self._reader["last_seen"],
                    "macOS": self._reader["macOS"],
                },
                "preview": {
                    "armed": armed,
                    "source_id": self._preview_source_id if armed else "",
                    "item": dict(self._preview_item) if self._preview_item else None,
                },
            }
