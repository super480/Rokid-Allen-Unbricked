"""Read-only Codex account queries through the local app-server RPC.

The Codex CLI is already signed in on this Mac. We start one short-lived
``codex app-server --listen stdio://`` child per query, speak line-delimited
JSON, and only ever call account reads. No thread, no turn, no model call,
no login/logout, no config writes, no token export.

Protocol order (from the implementation handover, verified on 0.164.0):
    initialize -> wait for its response -> initialized -> account reads
Notifications have no id and must never be mistaken for a response.
"""
from __future__ import annotations

import json
import math
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import date
from typing import Any

DEFAULT_EXECUTABLE = os.path.expanduser("~/.local/bin/codex")
WINDOW_LABELS = {300: "5h", 10080: "7d"}

INIT_TIMEOUT = 8.0
READ_TIMEOUT = 15.0
TOTAL_TIMEOUT = 30.0


class CodexError(Exception):
    """A query that cannot produce trustworthy data, with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def resolve_executable(configured: str | None = None) -> str:
    """Config first, then PATH, then the known local symlink."""
    for candidate in (configured, shutil.which("codex"), DEFAULT_EXECUTABLE):
        if candidate and os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


class CodexRpcClient:
    """One child process, one initialize, a handful of read-only calls."""

    def __init__(self, executable: str | None = None,
                 init_timeout: float = INIT_TIMEOUT,
                 read_timeout: float = READ_TIMEOUT,
                 total_timeout: float = TOTAL_TIMEOUT):
        self.executable = resolve_executable(executable)
        self.init_timeout = init_timeout
        self.read_timeout = read_timeout
        self.total_timeout = total_timeout

    def version(self) -> str:
        if not self.executable:
            return ""
        try:
            out = subprocess.run([self.executable, "--version"],
                                capture_output=True, text=True, timeout=5)
            return (out.stdout or "").strip().splitlines()[0] if out.stdout else ""
        except (OSError, subprocess.SubprocessError, IndexError):
            return ""

    def call(self, requests: list[tuple[str, dict]]) -> list[dict]:
        """Send read-only methods in one session; return each result dict."""
        if not self.executable:
            raise CodexError("unavailable", "本机找不到 Codex 命令行。")
        if not requests:
            return []
        proc = subprocess.Popen(
            [self.executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        inbox: queue.Queue = queue.Queue()

        def pump(stream, tag):
            try:
                for line in stream:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    try:
                        inbox.put((tag, json.loads(line)))
                    except ValueError:
                        # Non-JSON chatter must not become a response.
                        inbox.put((tag, None))
            except (OSError, ValueError):
                pass  # stream closed by the teardown below
            finally:
                inbox.put((tag, {"__eof__": True}))

        # stderr is drained continuously so a chatty child cannot fill the
        # pipe and deadlock. Contents are deliberately not logged.
        threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True).start()
        threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True).start()

        deadline = time.monotonic() + self.total_timeout

        def send(payload: dict) -> None:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()

        def wait_for(wanted: int, timeout: float) -> dict:
            left = max(0.05, min(timeout, deadline - time.monotonic()))
            end = time.monotonic() + left
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise CodexError("timeout", "Codex 读取超时。")
                try:
                    tag, message = inbox.get(timeout=remaining)
                except queue.Empty:
                    raise CodexError("timeout", "Codex 读取超时。")
                if tag == "err" or not isinstance(message, dict):
                    continue
                if message.get("__eof__"):
                    raise CodexError("upstream_error", "Codex 服务提前退出。")
                if message.get("id") != wanted:
                    continue  # notification
                if "error" in message:
                    raise CodexError("unsupported_method",
                                     "Codex 不支持该读取接口。")
                return message.get("result") or {}

        try:
            send({"id": 0, "method": "initialize",
                  "params": {"clientInfo": {"name": "rokid_miniapps",
                                            "version": "0.1.0"}}})
            wait_for(0, self.init_timeout)
            send({"method": "initialized"})
            results = []
            for index, (method, params) in enumerate(requests, start=1):
                send({"id": index, "method": method, "params": params})
                results.append(wait_for(index, self.read_timeout))
            return results
        except (OSError, BrokenPipeError) as error:
            raise CodexError("upstream_error", "Codex 服务无法通信：%s" % error)
        finally:
            # Only the child we created here is ever touched.
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


def normalise_account(result: dict) -> dict:
    """Login kind only; never keep the email or account id."""
    account = result.get("account") if isinstance(result, dict) else None
    if not isinstance(account, dict) or not account.get("type"):
        return {"type": "", "requires_auth": bool(
            isinstance(result, dict) and result.get("requiresOpenaiAuth"))}
    return {"type": str(account.get("type")), "requires_auth": False}


def normalise_rate_limits(result: dict) -> dict:
    """Pick the Codex bucket by id, then map windows by real duration.

    Never sums buckets, never guesses which window is primary, never turns a
    missing/anomalous field into zero, and never calls 10080 minutes a calendar
    week. Over-100 values are preserved (the device clamps the drawn arc).
    """
    if not isinstance(result, dict):
        result = {}
    by_id = result.get("rateLimitsByLimitId")
    bucket = None
    source = ""
    limit_ids = []
    if isinstance(by_id, dict) and by_id:
        limit_ids = sorted(str(key) for key in by_id)
        if "codex" in by_id and isinstance(by_id["codex"], dict):
            bucket, source = by_id["codex"], "by_limit_id"
    elif isinstance(result.get("rateLimits"), dict):
        compat = result["rateLimits"]
        limit_id = compat.get("limitId")
        if limit_id in (None, "codex"):
            bucket, source = compat, "compat"
        else:
            limit_ids = [str(limit_id)]

    if bucket is None:
        return {"windows": [], "unrecognized": [], "limit_id": None,
                "source": source, "limit_ids": limit_ids,
                "plan_type": None,
                "error_code": "unsupported_window",
                "field_error": False}

    windows = []
    unrecognized = []
    field_error = False
    for slot in ("primary", "secondary"):
        raw = bucket.get(slot)
        if not isinstance(raw, dict):
            continue
        percent = _finite_number(raw.get("usedPercent"))
        duration = raw.get("windowDurationMins")
        duration = int(duration) if isinstance(duration, int) and not isinstance(duration, bool) else None
        reset = raw.get("resetsAt")
        reset_seconds = int(reset) if isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(float(reset)) else None
        if percent is None or percent < 0:
            field_error = True
            continue
        label = WINDOW_LABELS.get(duration) if duration is not None else None
        entry = {
            "slot": slot,
            "label": label or (str(duration) + "m" if duration else "?"),
            "duration_minutes": duration,
            "value": percent,
            "used_percent": percent,
            "over_limit": percent > 100,
            "reset_at_seconds": reset_seconds,
            "reset_at_ms": reset_seconds * 1000 if reset_seconds else None,
        }
        if label is None:
            unrecognized.append(entry)
        else:
            windows.append(entry)

    # Outer ring is the longer window regardless of primary/secondary order.
    windows.sort(key=lambda item: item["duration_minutes"] or 0, reverse=True)
    return {
        "windows": windows,
        "unrecognized": unrecognized,
        "limit_id": bucket.get("limitId") or "codex",
        "source": source,
        "limit_ids": limit_ids,
        "plan_type": bucket.get("planType"),
        "error_code": ("unsupported_window" if unrecognized and not windows
                       else None),
        "field_error": field_error,
    }


def normalise_usage(result: dict) -> dict:
    """Server-side token statistics. Ints become decimal strings.

    A missing date is never a zero-usage day. ``today_tokens`` is only filled
    when the server actually returned a bucket for this machine's date.
    """
    if not isinstance(result, dict):
        result = {}
    summary = result.get("summary") or {}
    lifetime = summary.get("lifetimeTokens") if isinstance(summary, dict) else None
    field_error = False
    if isinstance(lifetime, int) and not isinstance(lifetime, bool):
        lifetime_text = str(lifetime)
    elif lifetime is None:
        lifetime_text = None
    else:
        lifetime_text, field_error = None, True

    buckets = []
    raw_buckets = result.get("dailyUsageBuckets")
    if isinstance(raw_buckets, list):
        for item in raw_buckets:
            if not isinstance(item, dict):
                field_error = True
                continue
            day, tokens = item.get("startDate"), item.get("tokens")
            if not isinstance(day, str) or not (isinstance(tokens, int)
                                                and not isinstance(tokens, bool)):
                field_error = True
                continue
            buckets.append({"date": day, "tokens": str(tokens)})
    dates = [row["date"] for row in buckets]
    latest = max(dates) if dates else None
    today = date.today().isoformat()
    today_tokens = next((row["tokens"] for row in buckets
                         if row["date"] == today), None)
    return {
        "lifetime_tokens": lifetime_text,
        "daily_buckets": buckets,
        "latest_bucket_date": latest,
        "today_date": today,
        "today_tokens": today_tokens,
        "field_error": field_error,
    }


class CodexUsageProvider:
    """The only place that talks to Codex. Returns plain normalised dicts."""

    def __init__(self, client: CodexRpcClient | None = None):
        self.client = client or CodexRpcClient()

    def read(self, operation: str) -> dict:
        methods: list[tuple[str, dict]] = [
            ("account/read", {"refreshToken": False})]
        if operation in ("quota", "overview"):
            methods.append(("account/rateLimits/read", {}))
        if operation in ("tokens", "overview"):
            methods.append(("account/usage/read", {}))
        results = self.client.call(methods)
        account = normalise_account(results[0])
        if not account["type"]:
            raise CodexError("not_logged_in", "请在 Mac 上登录 Codex。")
        if account["type"] != "chatgpt":
            raise CodexError("unsupported_auth",
                             "当前 Codex 登录方式不提供套餐用量。")
        payload = {"account_type": account["type"]}
        cursor = 1
        if operation in ("quota", "overview"):
            payload["rate_limits"] = normalise_rate_limits(results[cursor])
            cursor += 1
        if operation in ("tokens", "overview"):
            payload["usage"] = normalise_usage(results[cursor])
        return payload
