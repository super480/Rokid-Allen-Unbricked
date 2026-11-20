"""Turn one normalised provider payload into a card + a spoken summary.

Both outputs come from the same QueryResult, so the number on the screen and
the number that is read out can never disagree. No images are produced: the
device draws the V6 double-ring layout itself from this JSON and owns the
countdown, so the card stays live for its whole lifetime.

The user's 2026-11-15 review changed the meaning of the arcs: they show the
REMAINING quota, not the used quota. The V6 preview assets still say "已用"
in their README/index.html; this implementation follows the later decision.
`used_percent` is kept in each ring as data, while `value` is what is drawn.

V6 geometry, uniformly shrunk to 7/8 (user decision 2026-11-15):
    viewBox 640x480, centre (320,240), outer ring r=138 #0987ff,
    inner ring r=96 #00bfed, 270-degree arc starting at 135deg.
    Both strokes are 30px (equal weight, same-night user revision);
    V6 was r=158/110 with 37/31 strokes. The device draws the centre logo
    at a fixed 94x94, so it deliberately does not shrink with the rings.
"""
from __future__ import annotations

import time
from typing import Any

from .models import QueryResult, now_iso, validate_card
from .codex_usage import CodexError

# V6 used r=158/110 with 37/31 strokes. User decision 2026-11-15: shrink
# both rings uniformly to 7/8 (same ratio) while the 94x94 centre logo stays
# exactly the same size. Later the same night the user asked for equal stroke
# weight, so 32/27 -> 30/30 (the midpoint; the gap between rings stays ~12px
# because the radii differ by 42). The device reads radius/width from this
# layout, so changing the geometry never needs a new APK.
OUTER = {"radius": 138, "width": 30, "color": "#0987ff"}
INNER = {"radius": 96, "width": 30, "color": "#00bfed"}
# The plain OpenAI/GPT knot, not the app-icon with its white rounded plate.
ICON = "system-chip.png"
# Brighter than V6's #0d254f: the user could not see the track in daylight.
TRACK_COLOR = "#315a96"
SOURCE = "codex_app_server"


def remaining(used_percent: float) -> float:
    """Arc value: quota left, clamped at zero for over-limit windows."""
    return max(0.0, min(100.0, 100.0 - float(used_percent)))

_CN_DIGITS = "零一二三四五六七八九"


def cn_number(value: int) -> str:
    """0-999 in spoken Chinese; keeps summaries readable out loud."""
    value = int(value)
    if value < 0 or value >= 1000:
        return str(value)
    if value < 10:
        return _CN_DIGITS[value]
    if value < 20:
        return "十" + (_CN_DIGITS[value % 10] if value % 10 else "")
    if value < 100:
        tens, ones = divmod(value, 10)
        return _CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")
    hundreds, rest = divmod(value, 100)
    head = _CN_DIGITS[hundreds] + "百"
    if not rest:
        return head
    if rest < 10:
        return head + "零" + _CN_DIGITS[rest]
    return head + cn_number(rest)


def human_tokens(value: str | None) -> str:
    if value is None:
        return "—"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "—"
    if number >= 100_000_000:
        return "%.2f 亿" % (number / 100_000_000)
    if number >= 10_000:
        return "%.1f 万" % (number / 10_000)
    return "{:,}".format(number)


def reset_brief(reset_seconds: int | None, now: float | None = None) -> str:
    """Countdown text for the card. Never claims a reset actually happened."""
    if reset_seconds is None:
        return "—"
    left = int(reset_seconds - (now if now is not None else time.time()))
    if left <= 0:
        return "等待更新"
    days, rest = divmod(left, 86400)
    hours, minutes = divmod(rest, 3600)[0], divmod(rest, 3600)[1] // 60
    if days:
        return "%dd %02dh" % (days, hours)
    return "%dh %02dm" % (hours, minutes)


def _reset_speech(reset_seconds: int | None, now: float) -> str:
    if reset_seconds is None:
        return ""
    left = int(reset_seconds - now)
    if left <= 0:
        return "，重置时间已到，等待数据更新"
    days, rest = divmod(left, 86400)
    hours = rest // 3600
    minutes = (rest % 3600) // 60
    if days:
        return "，约%s天%s小时后重置" % (cn_number(days), cn_number(hours))
    if hours:
        return "，约%s小时%s分后重置" % (cn_number(hours), cn_number(minutes))
    return "，约%s分后重置" % cn_number(max(1, minutes))


def _footer(result_status: str, fetched_at: str, extra: str = "") -> str:
    clock = fetched_at[11:16] if len(fetched_at) >= 16 else fetched_at[:5]
    label = {"ok": "", "partial": " · 部分数据", "stale": " · 缓存",
             "error": " · 不可用", "loading": " · 读取中"}.get(result_status, "")
    return ("采样 %s%s%s · Codex App Server" % (clock, label, extra))[:120]


def _base_card(template: str, title: str, status: str, fetched_at: str,
               rows: list[dict], notice: str = "") -> dict:
    card = {
        "template": template,
        "title": title,
        "icon": ICON,
        "rows": rows,
        "rings": [],
        "footer": _footer(status, fetched_at),
        "notice": notice,
        "status": status,
    }
    validate_card(card)
    return card


def quota_card(windows: list[dict], status: str, fetched_at: str,
               rows_extra: list[dict] | None = None,
               notice: str = "", now: float | None = None) -> dict:
    rings = []
    rows = []
    for index, window in enumerate(windows[:2]):
        preset = OUTER if index == 0 else INNER
        rings.append({
            "label": window["label"],
            # Drawn arc = remaining quota. Kept in 0..100: an over-limit
            # window is an empty arc plus the spoken "超出额度" warning.
            "value": remaining(window["used_percent"]),
            # Original upstream value, kept for data/debugging only.
            "used_percent": window["used_percent"],
            "color": preset["color"],
            "radius": preset["radius"],
            "width": preset["width"],
            "reset_at_ms": window.get("reset_at_ms"),
        })
        rows.append({"label": window["label"],
                     "value": reset_brief(window.get("reset_at_seconds"), now)})
    if rows_extra:
        rows.extend(rows_extra)
    card = {
        "template": "rings",
        "title": "Codex 额度",
        "icon": ICON,
        "track_color": TRACK_COLOR,
        "rows": rows[:6],
        "rings": rings,
        "footer": _footer(status, fetched_at),
        "notice": notice,
        "status": status,
    }
    validate_card(card)
    return card


def lines_card(title: str, rows: list[dict], status: str, fetched_at: str,
               notice: str = "") -> dict:
    return _base_card("lines", title, status, fetched_at, rows[:6], notice)


def build_result(operation: str, payload: dict, request_id: str = "",
                 now: float | None = None) -> QueryResult:
    """One provider payload -> one QueryResult with its card and speech."""
    moment = now if now is not None else time.time()
    fetched_at = now_iso(moment)
    limits = payload.get("rate_limits")
    usage = payload.get("usage")

    if operation == "quota":
        return _quota_result(limits, request_id, fetched_at, moment)
    if operation == "tokens":
        return _tokens_result(usage, request_id, fetched_at, moment)
    return _overview_result(limits, usage, request_id, fetched_at, moment)


def _quota_result(limits: dict | None, request_id: str, fetched_at: str,
                  now: float) -> QueryResult:
    if not limits or not limits["windows"]:
        reason = (limits or {}).get("error_code") or "field_error"
        speech = "当前没有返回 Codex 标准额度窗口，无法读取用量。"
        if limits and limits.get("unrecognized"):
            speech = "Codex 返回的窗口时长不是标准的五小时或七天，暂时无法上屏。"
        result = QueryResult(
            app_id="codex_usage", operation="quota", status="error",
            error_code=reason if reason in (
                "unsupported_window", "field_error") else "unavailable",
            source=SOURCE, speech=speech, fetched_at=fetched_at,
            request_id=request_id,
            data={"windows": [], "unrecognized": (limits or {}).get("unrecognized", [])},
        )
        result.data["card"] = lines_card(
            "Codex 额度", [{"label": "状态", "value": "窗口不可用"}],
            "error", fetched_at, speech)
        return result

    windows = limits["windows"]
    parts = []
    for window in windows:
        label = "七天" if window["label"] == "7d" else "五小时"
        percent = int(round(remaining(window["used_percent"])))
        parts.append("%s额度剩余百分之%s" % (label, cn_number(percent)))
    speech = "Codex " + "，".join(parts) + "。"
    if any(window["over_limit"] for window in windows):
        speech += "其中一项已超出额度。"
    soonest = min((window for window in windows
                   if window.get("reset_at_seconds")),
                  key=lambda item: item["reset_at_seconds"], default=None)
    if soonest:
        label = "七天" if soonest["label"] == "7d" else "五小时"
        speech += label + "窗口" + _reset_speech(soonest["reset_at_seconds"], now).lstrip("，") + "。"
    speech = speech[:100]

    status = "ok"
    notice = ""
    if limits.get("field_error"):
        status, notice = "partial", "部分窗口字段异常，已略过"
    if limits.get("unrecognized"):
        status = "partial"
        notice = (notice + " 另有非标准时长窗口未上屏").strip()
    if limits.get("source") == "compat":
        status = "partial"
        notice = (notice + " 兼容视图来源").strip()

    result = QueryResult(
        app_id="codex_usage", operation="quota", status=status,
        source=SOURCE, speech=speech, fetched_at=fetched_at,
        request_id=request_id,
        data={"windows": windows, "limit_id": limits.get("limit_id"),
              "source": limits.get("source"),
              "unrecognized": limits.get("unrecognized", [])},
    )
    result.data["card"] = quota_card(windows, status, fetched_at,
                                     notice=notice, now=now)
    result.validate()
    return result


def cn_date(day: str | None) -> str:
    if not day or len(day) < 10:
        return "未知日期"
    try:
        month, date_part = int(day[5:7]), int(day[8:10])
    except ValueError:
        return day
    return "%s月%s日" % (cn_number(month), cn_number(date_part))


def _tokens_result(usage: dict | None, request_id: str, fetched_at: str,
                   now: float) -> QueryResult:
    if not usage or usage.get("lifetime_tokens") is None:
        speech = "暂时读不到 Codex 的 token 统计。"
        result = QueryResult(
            app_id="codex_usage", operation="tokens", status="error",
            error_code="field_error", source=SOURCE, speech=speech,
            fetched_at=fetched_at, request_id=request_id,
            data={"lifetime_tokens": None, "daily_buckets": []},
        )
        result.data["card"] = lines_card(
            "Codex token 用量", [{"label": "状态", "value": "统计不可用"}],
            "error", fetched_at, speech)
        return result

    lifetime = usage["lifetime_tokens"]
    latest = usage.get("latest_bucket_date")
    today_tokens = usage.get("today_tokens")
    rows = [
        {"label": "账户累计", "value": human_tokens(lifetime) + " token"},
        {"label": "最新日统计",
         "value": (cn_date(latest) + " · " + human_tokens(
             _bucket_tokens(usage, latest))) if latest else "无日统计"},
        {"label": "今日",
         "value": human_tokens(today_tokens) + " token"
         if today_tokens is not None else "暂无今天的统计"},
    ]
    speech = "Codex 账户累计使用 %s token。" % human_tokens(lifetime)
    if today_tokens is None:
        speech += "目前没有今天的 token 统计"
        if latest:
            speech += "，最新日统计到" + cn_date(latest)
        speech += "。"
    else:
        speech += "今天已统计 %s token。" % human_tokens(today_tokens)
    speech = speech[:100]

    status = "partial" if usage.get("field_error") else "ok"
    notice = "日统计按服务端日期，未确认时区" if latest else ""
    result = QueryResult(
        app_id="codex_usage", operation="tokens", status=status,
        source=SOURCE, speech=speech, fetched_at=fetched_at,
        data_through=latest, request_id=request_id,
        data={"lifetime_tokens": lifetime,
              "daily_buckets": usage.get("daily_buckets", []),
              "latest_bucket_date": latest,
              "today_tokens": today_tokens,
              "today_date": usage.get("today_date")},
    )
    result.data["card"] = lines_card(
        "Codex token 用量", rows, status, fetched_at, notice)
    result.validate()
    return result


def _bucket_tokens(usage: dict, day: str) -> str | None:
    for row in usage.get("daily_buckets", []):
        if row.get("date") == day:
            return row.get("tokens")
    return None


def _overview_result(limits: dict | None, usage: dict | None,
                     request_id: str, fetched_at: str,
                     now: float) -> QueryResult:
    quota = _quota_result(limits, request_id, fetched_at, now)
    tokens = _tokens_result(usage, request_id, fetched_at, now)
    if quota.status == "error":
        return tokens
    extra = []
    if usage and usage.get("lifetime_tokens"):
        extra.append({"label": "累计",
                      "value": human_tokens(usage["lifetime_tokens"]) + " token"})
    card = quota_card((limits or {}).get("windows", []),
                      "partial" if (quota.status != "ok"
                                    or tokens.status != "ok") else "ok",
                      fetched_at, rows_extra=extra[:2],
                      notice=quota.data["card"].get("notice", ""), now=now)
    result = QueryResult(
        app_id="codex_usage", operation="overview",
        status="partial" if (quota.status != "ok" or tokens.status != "ok") else "ok",
        source=SOURCE,
        speech=((quota.speech.split("。")[0] + "。"
                 + ("累计使用 %s token。" % human_tokens(
                     usage["lifetime_tokens"]) if usage and usage.get(
                         "lifetime_tokens") else "")))[:100],
        fetched_at=fetched_at, data_through=tokens.data_through,
        request_id=request_id,
        data={"quota": quota.data, "tokens": tokens.data},
    )
    result.data["card"] = card
    result.validate()
    return result


def error_result(operation: str, error: CodexError,
                 request_id: str = "") -> QueryResult:
    messages = {
        "not_logged_in": "请在 Mac 上登录 Codex，之后就能查询用量。",
        "unsupported_auth": "当前 Codex 登录方式不提供套餐用量。",
        "unsupported_method": "本机 Codex 版本不支持这个读取接口。",
        "unsupported_window": "当前没有返回 Codex 标准额度窗口。",
        "timeout": "Codex 读取超时，请稍后再试。",
        "upstream_error": "Codex 服务暂时无法读取。",
        "unavailable": "本机找不到 Codex 命令行。",
    }
    speech = messages.get(error.code, "用量暂时无法读取。")
    fetched_at = now_iso()
    result = QueryResult(
        app_id="codex_usage", operation=operation, status="error",
        error_code=error.code if error.code in messages else "upstream_error",
        source=SOURCE, speech=speech, fetched_at=fetched_at,
        request_id=request_id, data={"error": error.message},
    )
    result.data["card"] = lines_card(
        "Codex 用量", [{"label": "状态", "value": "读取失败"}],
        "error", fetched_at, speech)
    result.validate()
    return result
