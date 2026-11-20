"""Rokid bridge plugin for N.E.K.O. (user-installed, v0.9.1 SDK).

The desktop client owns the character's single WebSocket. Upstream accepts a
new `/ws/{character}` socket with "newest socket always wins" and closes the
older one with ``CHARACTER_SWITCHING_TERMINAL``; replies go only to that one
socket. So the Rokid host must NOT open a second WebSocket.

Instead this plugin runs inside N.E.K.O.'s own plugin process and calls
``ctx.push_message(visibility=["chat"], ai_behavior="respond", ...)`` for each
line the speaker picked up. The desktop client therefore starts the turn
itself: the user's line is visible in its chat history, the reply is displayed
there and read aloud by the desktop TTS. The Rokid host stays a microphone and
a screen.

Endpoints (loopback, 127.0.0.1:48917):
    GET  /health                 -> {"ok": true}
    GET  /last-reply             -> {"fingerprint": "...", "text": "..."}
    POST /input {"text", "request_id"} -> {"ok": true}

``/last-reply`` is only a read-only mirror of the newest assistant line so the
host can put the reply on the speaker's subtitle strip. It is NOT the reply
channel -- the desktop client receives the real turn from the server.

Modelled on the bundled ``claude_companion`` plugin (HTTP hook -> push_message).
Every startup/IO path is defensive: a broken bridge must never affect the
desktop app.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from plugin.sdk.plugin import NekoPluginBase, lifecycle, neko_plugin, Ok

_DEFAULT_PORT = 48917
_MAX_TEXT = 4000
_TS_PREFIX = re.compile(r"^\[\d{8}\s+[^\]]*\]\s*")


def _reply_text(row: dict[str, Any]) -> str:
    """Pull the newest assistant text out of a memory ``recent.json`` row."""
    data = row.get("data")
    if isinstance(data, str):
        try:
            data = ast.literal_eval(data)
        except (ValueError, SyntaxError):
            return ""
    if not isinstance(data, dict):
        return ""
    content = data.get("content")
    if isinstance(content, str):
        return _TS_PREFIX.sub("", content).strip()
    if not isinstance(content, list):
        return ""
    parts = [item.get("text", "") for item in content
             if isinstance(item, dict) and isinstance(item.get("text"), str)]
    return _TS_PREFIX.sub("", "".join(parts)).strip()


class _Handler(BaseHTTPRequestHandler):
    plugin: Optional["RokidBridgePlugin"] = None

    def log_message(self, *_args: Any) -> None:
        return  # the desktop log is noisy enough already

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _authorized(self) -> bool:
        plugin = self.plugin
        if plugin is None or not plugin.token:
            return True  # loopback-only is the default
        return self.headers.get("Authorization", "") == "Bearer " + plugin.token

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if self.path.split("?", 1)[0] == "/health":
            self._send(200, {"ok": True, "plugin": "rokid_bridge"})
            return
        if self.path.split("?", 1)[0] == "/last-reply":
            self._send(200, self.plugin.latest_reply() if self.plugin
                       else {"fingerprint": "", "text": ""})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if self.path.split("?", 1)[0] != "/input":
            self._send(404, {"error": "not found"})
            return
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 64 * 1024)
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._send(400, {"error": "invalid JSON"})
            return
        text = str(data.get("text") or "").strip()[:_MAX_TEXT]
        if not text:
            self._send(400, {"error": "empty text"})
            return
        if self.plugin is None or not self.plugin.inject(
                text, str(data.get("request_id") or "")):
            self._send(503, {"error": "push_message unavailable"})
            return
        self._send(200, {"ok": True})


@neko_plugin
class RokidBridgePlugin(NekoPluginBase):
    """Turn speaker transcripts into desktop-owned chat turns."""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        # ctx.logger is a proxy that talks back to the host process; using it
        # from a lifecycle hook can stall startup past the 10s deadline. The
        # SDK's file logger is local, exactly like the bundled plugins use.
        try:
            self.logger = self.enable_file_logging(log_level="INFO")
        except Exception:
            self.logger = None
        self.token = ""
        self.port = _DEFAULT_PORT
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._memory_root = (Path.home() / "Library" / "Application Support"
                             / "N.E.K.O" / "memory")

    def _log(self, level: str, message: str, *args: Any) -> None:
        """Logging must never hold up a lifecycle call or an injected turn."""
        try:
            if self.logger is not None:
                getattr(self.logger, level)(message, *args)
        except Exception:
            pass

    @lifecycle(id="startup")
    async def startup(self, **_kwargs: Any):
        # The SDK treats the startup return value as the "ready" signal. The
        # bundled plugins all return Ok({...}); returning None leaves the host
        # waiting until PLUGIN_START_TIMEOUT (10s). Keep every RPC bounded so
        # a slow config service can never eat that budget either.
        try:
            import asyncio
            cfg = await asyncio.wait_for(
                self.config.dump(timeout=1.5), timeout=2.5)
            if isinstance(cfg, dict):
                rokid = cfg.get("rokid") or {}
                self.port = int(rokid.get("port") or _DEFAULT_PORT)
                self.token = str(rokid.get("token") or "")
        except Exception as exc:
            self._log("warning", "rokid_bridge config read failed: {}", exc)
        self._start()
        return Ok({"status": "running", "port": self.port})

    @lifecycle(id="shutdown")
    def shutdown(self, **_kwargs: Any):
        self._stop()
        return Ok({"status": "shutdown"})

    def _start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            try:
                _Handler.plugin = self
                self._server = ThreadingHTTPServer(("127.0.0.1", self.port),
                                                   _Handler)
            except Exception as exc:
                self._server = None
                self._log("error", "rokid_bridge HTTP bind failed: {}", exc)
                return
            server = self._server
        self._thread = threading.Thread(target=server.serve_forever,
                                        name="rokid-bridge-http",
                                        daemon=True)
        self._thread.start()
        self._log("info", "rokid_bridge listening on 127.0.0.1:{}", self.port)

    def _stop(self) -> None:
        with self._lock:
            server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass

    def inject(self, text: str, request_id: str) -> bool:
        """Hand one speaker line to the desktop chat as a visible user turn."""
        try:
            if not hasattr(self.ctx, "push_message"):
                self._log("error", "rokid_bridge: ctx.push_message missing")
                return False
            self.ctx.push_message(
                source="rokid_bridge",
                visibility=["chat"],
                ai_behavior="respond",
                parts=[{"type": "text", "text": text}],
                priority=5,
                metadata={"rokid": True, "rokid_request_id": request_id},
            )
            return True
        except Exception as exc:
            self._log("error", "rokid_bridge push failed: {}", exc)
            return False

    def latest_reply(self) -> dict[str, str]:
        """Read-only mirror of the newest assistant line, for the Rokid strip.

        Uses the most recently modified character memory file. A fingerprint
        lets the host tell a genuinely new reply from the previous one without
        this plugin having to observe the turn itself.
        """
        try:
            files = [p for p in self._memory_root.glob("*/recent.json")
                     if p.is_file()]
            if not files:
                return {"fingerprint": "", "text": ""}
            newest = max(files, key=lambda p: p.stat().st_mtime)
            rows = json.loads(newest.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                return {"fingerprint": "", "text": ""}
            for row in reversed(rows):
                if isinstance(row, dict) and row.get("type") == "ai":
                    text = _reply_text(row)
                    if text:
                        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
                        return {"fingerprint": digest, "text": text}
        except Exception:
            pass
        return {"fingerprint": "", "text": ""}
