"""猫娘 backend in relay mode — no WebSocket, the desktop client owns the turn.

Upstream N.E.K.O. keeps ONE live socket per character: every new
``/ws/{character}`` connection replaces ``session_id`` and
``mgr.websocket`` ("newest socket always wins"), and the older socket is
closed with ``CHARACTER_SWITCHING_TERMINAL`` as soon as it sends a
non-voice message. Replies go only to that single socket. Letting the host
open its own socket therefore kicks the Mac window out of text input and
leaves its chat history out of sync.

Relay mode removes the host's socket entirely. Each speaker line is POSTed to
the locally installed ``rokid_bridge`` N.E.K.O. plugin, which calls N.E.K.O.'s
own ``ctx.push_message(visibility=["chat"], ai_behavior="respond", ...)`` from
inside the upstream process. The desktop client then starts the turn itself,
so the user's line appears in the shared chat history and the reply is shown
and read aloud by the desktop TTS. The speaker stays a microphone and a
screen.

``GET /last-reply`` is a read-only mirror used only to put the reply on the
speaker's subtitle strip. It is polled against a fingerprint taken before the
line was submitted, so a stale reply can never be mistaken for this turn.

See docs/research/neko-ws-conflict-assessment.md.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from backends.base import Backend, BackendError, report

DEFAULT_URL = "http://127.0.0.1:48917"


class NekoRelayBackend(Backend):
    """One assistant slot backed by the loopback bridge plugin."""

    id = "neko"
    name = "猫娘"

    def __init__(self, settings=None, state_store=None):
        settings = settings or {}
        relay = dict(settings.get("relay") or {})
        self.url = str(relay.get("url") or DEFAULT_URL).rstrip("/")
        self.token = str(relay.get("token") or "")
        try:
            self.timeout = float(relay.get("timeout")
                                 or settings.get("timeout") or 60)
        except (TypeError, ValueError):
            self.timeout = 60.0
        try:
            self.poll = max(0.2, float(relay.get("poll_interval") or 0.6))
        except (TypeError, ValueError):
            self.poll = 0.6
        self._lock = threading.Lock()
        self._cancelled = set()
        # Same shape as NekoBackend so the engine's optional consumers stay
        # happy. Relay mode never produces unsolicited frames of its own.
        self.proactive = []

    # -- loopback HTTP ---------------------------------------------------------

    def _request(self, path, payload=None, timeout=5.0):
        url = self.url + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise BackendError("若琪桥接插件 HTTP %s：%s"
                               % (error.code, path)) from error
        except (OSError, ValueError) as error:
            raise BackendError("若琪桥接插件没响应：" + str(error)) from error

    def _last_reply(self):
        row = self._request("/last-reply", timeout=3.0)
        if not isinstance(row, dict):
            return "", ""
        return (str(row.get("fingerprint") or ""),
                str(row.get("text") or ""))

    # -- Backend contract -----------------------------------------------------

    def run(self, text, request_id, on_progress=None, memory_text=None):
        report(on_progress, "thinking", "猫娘在想")
        before, _ = self._last_reply()
        self._request("/input", {"text": str(text),
                                 "request_id": str(request_id)}, timeout=8.0)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            with self._lock:
                if request_id in self._cancelled:
                    self._cancelled.discard(request_id)
                    return ""
            fingerprint, answer = self._last_reply()
            if answer and fingerprint != before:
                report(on_progress, "speaking", "猫娘在说话")
                return answer
            time.sleep(self.poll)
        raise BackendError("猫娘在 " + ("%g" % self.timeout) + " 秒内没有回答")

    def cancel(self, request_id):
        with self._lock:
            self._cancelled.add(request_id)

    def health(self):
        try:
            row = self._request("/health", timeout=3.0)
            if isinstance(row, dict) and row.get("ok"):
                return True, "ok"
            return False, "若琪桥接插件未就绪"
        except BackendError as error:
            return False, str(error)

    def close(self):
        with self._lock:
            self._cancelled.clear()
