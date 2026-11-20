"""Layouts for the two local Mac query apps (design/app-platform/system-cards-v2).

Both cards reuse the exact same ``miniapp_card.v1`` rings renderer as the
Codex quota card: host sends numbers/colours/rows, the device draws them
with native Canvas and owns the 900ms entrance animation. No PNG screenshot
is ever sent to the speaker.

Colours and wording come from the design agent's v2 master images:
    CPU/RAM  orange #ffa634 on a dark brown track, purple #a76dfd inside,
             chip-with-pulse icon, "CPU 36%" / "RAM 10.9 / 16 GB",
             footer "采样 21:38 · 系统资源".
    Network  green #07f2c6 download on a dark teal track, blue #2789ff
             upload on a dark blue track, up/down connector icon,
             "↓ 12.8 MB/s" / "↑ 2.4 MB/s",
             footer "采样 21:38 · 网络速度 · 满量程 20 MB/s".

The generated images are visual references: their arc lengths do not have
to match the numbers exactly. Geometry is the shipped 30/30 equal-weight
double ring (outer r=138, inner r=96), which the device draws precisely.
"""
from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any

from .models import QueryResult, now_iso, validate_card
from .system_metrics import clamp, format_memory, format_rate

SOURCE = "mac_local_metrics"

OUTER = {"radius": 138, "width": 30}
INNER = {"radius": 96, "width": 30}

CPU_COLOR = "#ffa634"
CPU_TRACK = "#6d4b2d"
MEM_COLOR = "#a76dfd"
MEM_TRACK = "#3b2b5e"          # dark violet; the generated master is noisy here

DOWN_COLOR = "#07f2c6"
DOWN_TRACK = "#0f6c5e"
UP_COLOR = "#2789ff"
UP_TRACK = "#305179"

CHIP_ICON = "system-chip.png"
NETWORK_ICON = "network-arrows.png"


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _footer(sampled_at: Any, text: str) -> str:
    stamp = _finite(sampled_at)
    clock = (datetime.fromtimestamp(stamp).strftime("%H:%M")
             if stamp and stamp > 0 else now_iso(time.time())[11:16])
    return ("采样 %s · %s" % (clock, text))[:120]


def _base_card(title: str, icon: str, rings: list[dict], rows: list[dict],
               footer: str, status: str, notice: str = "") -> dict:
    card = {
        "template": "rings",
        "title": title,
        "icon": icon,
        # Per-ring track_color overrides this card-level fallback (the two
        # rings use different dark tracks in the design). The device reads
        # ring.track_color first and falls back to track_color.
        "track_color": rings[0].get("track_color", CPU_TRACK),
        "rows": rows[:6],
        "rings": rings,
        "footer": footer,
        "notice": notice,
        "status": status,
    }
    validate_card(card)
    return card


def _ring(label: str, value: float, color: str, track: str,
          preset: dict, value_text: str = "") -> dict:
    ring = {
        "label": label,
        "value": clamp(value, 0.0, 100.0),
        "color": color,
        "track_color": track,
        "radius": preset["radius"],
        "width": preset["width"],
    }
    if value_text:
        ring["value_text"] = value_text
    return ring


def resources_result(payload: dict, request_id: str = "") -> QueryResult:
    cpu = _finite(payload.get("cpu_percent"))
    mem_percent = _finite(payload.get("mem_percent"))
    mem_used = _finite(payload.get("mem_used_bytes"))
    mem_total = _finite(payload.get("mem_total_bytes"))
    status = "ok"
    notes = []
    cpu_missing = cpu is None
    mem_missing = mem_percent is None
    if cpu_missing:
        status = "partial"
        notes.append("CPU 读数暂不可用")
    if mem_missing:
        status = "partial"
        notes.append("内存读数暂不可用")
    cpu = clamp(cpu if cpu is not None else 0.0, 0.0, 100.0)
    mem_percent = clamp(mem_percent if mem_percent is not None else 0.0,
                        0.0, 100.0)
    used_bytes = int(mem_used) if mem_used is not None else None
    total_bytes = int(mem_total) if mem_total is not None else None
    rows = [
        {"label": "CPU", "value": "—" if cpu_missing else "%d%%" % round(cpu)},
        {"label": "RAM", "value": format_memory(used_bytes, total_bytes)},
    ]
    card = _base_card(
        "系统资源", CHIP_ICON,
        # 2026-11-16 (user): the START-CAP names are single letters "C"
        # and "R". Three-letter "CPU"/"RAM" used to hang outside the 30px
        # band at the arc start. The two BOTTOM rows keep the full names.
        [_ring("C", cpu, CPU_COLOR, CPU_TRACK, OUTER),
         _ring("R", mem_percent, MEM_COLOR, MEM_TRACK, INNER)],
        rows, _footer(payload.get("sampled_at"), "系统资源"),
        status, "；".join(notes))
    speech = ("Mac CPU 占用 %d%%，内存 %s（%d%%）。"
              % (round(cpu), format_memory(used_bytes, total_bytes),
                 round(mem_percent)))[:100]
    result = QueryResult(
        app_id="system_resources", operation="resources", status=status,
        source=SOURCE, speech=speech, fetched_at=now_iso(
            _finite(payload.get("sampled_at")) or time.time()),
        request_id=request_id,
        data={"cpu_percent": cpu, "mem_percent": mem_percent,
              "mem_used_bytes": used_bytes, "mem_total_bytes": total_bytes,
              "card": card},
    )
    result.validate()
    return result


def speed_result(payload: dict, request_id: str = "") -> QueryResult:
    down = max(0.0, _finite(payload.get("down_bps")) or 0.0)
    up = max(0.0, _finite(payload.get("up_bps")) or 0.0)
    down_pct = clamp(_finite(payload.get("down_percent")) or 0.0, 0.0, 100.0)
    up_pct = clamp(_finite(payload.get("up_percent")) or 0.0, 0.0, 100.0)
    full = _finite(payload.get("full_scale_mbps")) or 20.0
    kbps = _finite(payload.get("full_scale_kbps"))
    scale_text = ("%g KB/s" % kbps if kbps is not None and kbps < 1024
                  else "%g MB/s" % full)
    interface = str(payload.get("interface") or "")
    status = "ok" if interface else "partial"
    notice = "" if interface else "未找到活跃网络接口"
    rows = [
        {"label": "↓", "value": format_rate(down)},
        {"label": "↑", "value": format_rate(up)},
    ]
    card = _base_card(
        "网络速度", NETWORK_ICON,
        # 2026-11-16 (user): the two arrows belong at the ARC START (the
        # "head" of the progress bar), not at the growing arc end.
        # value_text still suppresses the default "N%" endpoint caption;
        # the device now draws it upright at START_ANGLE instead.
        # An empty ring.label means no separate start-of-arc name.
        [_ring("", down_pct, DOWN_COLOR, DOWN_TRACK, OUTER, "↓"),
         _ring("", up_pct, UP_COLOR, UP_TRACK, INNER, "↑")],
        rows,
        _footer(payload.get("sampled_at"),
                "网络速度 · 满量程 " + scale_text),
        status, notice)
    speech = ("当前下载 %s，上传 %s。" % (format_rate(down),
                                    format_rate(up)))[:100]
    result = QueryResult(
        app_id="network_speed", operation="speed", status=status,
        source=SOURCE, speech=speech,
        fetched_at=now_iso(_finite(payload.get("sampled_at")) or time.time()),
        request_id=request_id,
        data={"down_bps": down, "up_bps": up, "down_percent": down_pct,
              "up_percent": up_pct, "full_scale_mbps": full,
              "full_scale_kbps": payload.get("full_scale_kbps"),
              "interface": interface, "card": card},
    )
    result.validate()
    return result


def build_result(operation: str, payload: dict,
                 request_id: str = "") -> QueryResult:
    if operation == "resources":
        return resources_result(payload, request_id)
    if operation == "speed":
        return speed_result(payload, request_id)
    raise ValueError("unknown system operation: %s" % operation)


def error_result(operation: str, code: str = "upstream_error",
                 message: str = "", request_id: str = "") -> QueryResult:
    title = "网络速度" if operation == "speed" else "系统资源"
    text = ("本机系统数据暂时无法读取。"
            if code in ("unavailable", "timeout", "upstream_error")
            else "本机系统数据读取异常。")
    fetched_at = now_iso()
    result = QueryResult(
        app_id="network_speed" if operation == "speed" else "system_resources",
        operation=operation or "resources", status="error",
        error_code=code if code in (
            "unavailable", "timeout", "upstream_error", "field_error")
        else "upstream_error",
        source=SOURCE, speech=text, fetched_at=fetched_at,
        request_id=request_id, data={"error": message[:120]},
    )
    card = {
        "template": "lines",
        "title": title,
        "icon": NETWORK_ICON if operation == "speed" else CHIP_ICON,
        "rows": [{"label": "状态", "value": "读取失败"}],
        "rings": [],
        "footer": _footer(time.time(), title),
        "notice": text,
        "status": "error",
    }
    validate_card(card)
    result.data["card"] = card
    result.validate()
    return result
