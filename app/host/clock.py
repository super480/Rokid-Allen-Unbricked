"""待机时钟的主机侧：语音换表盘 + 控制台卡片。

设备侧是 app/clock/（自研 idle 应用，占 type 4 坑位）。本模块只做两
件事：把「换个表盘」这类说法接住，经 adb 广播 SET_FACE 转给设备；把
控制台卡片的状态读出来 / 写回去。表盘本体（渲染、config.json、
SET_FACE 接收）全在设备端，见 app/clock/android/src/cn/rokid/clock/。

设计与 weather.py 同构：handle_text 拦截 + status/set_* 控制台方法。
"""
from __future__ import annotations

import os

import re
import subprocess
from app_store import is_installed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from miniapps.registry import (MAX_CUSTOM_PHRASE_CHARS,  # noqa: E402
                               MAX_CUSTOM_PHRASES_PER_APP, match_key,
                               split_custom_phrases)

PACKAGE = "com.rokid.app.r5ef240ab9f64416fa0c9c69efd8e347e"
ACTION_SET_FACE = PACKAGE + ".SET_FACE"
CONFIG_PATH = "/sdcard/RokidClock/config.json"

#: Built-in turns that cycle to the next face. Custom phrases from the
#: console card join these after the shared match_key normalisation.
BUILTIN_FACES = ("grid", "digital", "orbit", "weekday", "minimal", "lagoon", "milk", "forest", "roman")

BUILTIN_KEYWORDS = ("换个表盘", "下一个表盘", "切换表盘", "换个时钟样式",
                    "换个时钟")
PHRASE = re.compile(r"(表盘|换个时钟|时钟样式)")


def is_clock_query(text: str) -> bool:
    return bool(PHRASE.search(str(text or "")))


def _adb_path(config: dict) -> Path:
    if os.environ.get("ROKID_ADB"):
        return Path(os.environ["ROKID_ADB"])
    here = Path(__file__).resolve().parent.parent.parent
    candidate = here / "tools" / "platform-tools" / "adb"
    return candidate if candidate.exists() else Path("adb")


class ClockService:
    """Voice entry + console card for the standby clock."""

    def __init__(self, engine, config: dict | None = None):
        self.engine = engine
        self.settings = config or {}

    # -- voice ------------------------------------------------------------

    @property
    def voice_phrases(self) -> list[str]:
        values = self.settings.get("voice_phrases") or []
        return [str(v) for v in values if str(v or "").strip()]

    def _custom_hit(self, text: str) -> bool:
        key = match_key(text)
        return any(key == match_key(phrase) for phrase in self.voice_phrases)

    def handle_text(self, text: str, request_id: str = "") -> dict | None:
        """None passes the turn on; a dict means the face was switched."""
        if not is_installed(self.engine, "clock"):
            return None
        spoken = str(text or "")
        if not (self._custom_hit(spoken) or is_clock_query(spoken)):
            return None
        self._broadcast("next")
        self.engine.log_transcript("user", "", spoken, request_id)
        self.engine.log_transcript(
            "agent", "", "（表盘已切换）", request_id)
        return {"accepted": True, "intent": "clock", "switched": True}

    def _broadcast(self, face: str) -> bool:
        config = self.engine.config or {}
        serial = (config.get("device") or "").strip()
        if not serial:
            print("clock: no device serial in config", flush=True)
            return False
        shell = "am broadcast -a %s --es face %s" % (ACTION_SET_FACE, face)
        try:
            result = subprocess.run(
                [str(_adb_path(config)), "-s", serial, "shell", shell],
                capture_output=True, text=True, timeout=15)
            note = (result.stdout or result.stderr or "").strip()
            print("clock broadcast: " + (note or "ok"), flush=True)
            return "result=0" in note or "Broadcast completed" in note
        except Exception as error:
            print("clock broadcast failed: %s" % error, flush=True)
            return False

    def set_available(self, enabled):
        serial = (self.engine.config.get("device") or "").strip()
        if not serial:
            return  # Offline/test host has no device side effect.
        command = "pm %s %s" % ("enable" if enabled else "disable", PACKAGE)
        result = subprocess.run([str(_adb_path(self.engine.config)), "-s", serial,
                                 "shell", command], capture_output=True, text=True, timeout=15)
        note = (result.stdout or "") + (result.stderr or "")
        expected = "enabled" if enabled else "disabled"
        if result.returncode != 0 or "new state: " + expected not in note:
            raise RuntimeError("设备时钟启停失败，请检查若琪连接后重试")

    def _read_device_face(self) -> str:
        config = self.engine.config or {}
        serial = (config.get("device") or "").strip()
        if not serial:
            return str(self.settings.get("face") or "grid")
        try:
            result = subprocess.run(
                [str(_adb_path(config)), "-s", serial, "shell",
                 "cat " + CONFIG_PATH],
                capture_output=True, text=True, timeout=10)
            match = re.search(r'"face"\s*:\s*"([^"]+)"', result.stdout or "")
            if match:
                return match.group(1)
        except Exception as error:
            print("clock read failed: %s" % error, flush=True)
        return str(self.settings.get("face") or "grid")

    # -- console card (设置 · 小应用 · 待机时钟) ---------------------------

    def status(self) -> dict:
        """What the console card needs on open."""
        return {
            "face": self._read_device_face(),
            "faces": list(dict.fromkeys(list(BUILTIN_FACES)
                          + list(self.settings.get("faces") or []))),
            "voice_phrases": self.voice_phrases,
            "builtin_phrases": list(BUILTIN_KEYWORDS),
            "builtin_mode": "keywords",
        }

    def _persist(self) -> None:
        save = getattr(self.engine, "_save_config", None)
        if callable(save):
            save()

    def set_face(self, name) -> dict:
        """Switch the face now (name or the literal 'next')."""
        if not is_installed(self.engine, "clock"):
            raise ValueError("请先在应用商店添加待机时钟")
        wanted = str(name or "").strip()
        if not wanted:
            raise ValueError("表盘名不能为空")
        self.settings["face"] = wanted
        self._persist()
        self._broadcast(wanted)
        return self.status()

    def set_voice_phrases(self, phrases) -> dict:
        """Save the card's custom spoken phrases; empty clears them.

        Same normalisation as the mini-apps and the weather card (shared
        ``match_key``, 12 phrases / 40 characters at most).
        """
        if isinstance(phrases, str):
            phrases = split_custom_phrases(phrases)
        seen: set[str] = set()
        cleaned: list[str] = []
        for value in list(phrases or []):
            phrase = str(value or "").strip()
            key = match_key(phrase)
            if (not phrase or not key
                    or len(phrase) > MAX_CUSTOM_PHRASE_CHARS
                    or key in seen):
                continue
            seen.add(key)
            cleaned.append(phrase)
            if len(cleaned) >= MAX_CUSTOM_PHRASES_PER_APP:
                break
        self.settings["voice_phrases"] = cleaned
        self._persist()
        return {"ok": True, "phrases": cleaned, "voice_phrases": cleaned}
