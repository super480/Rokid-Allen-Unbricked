"""猫娘 (Project N.E.K.O.) backend — the fifth assistant.

The other four backends drive a desktop CLI; this one talks to the N.E.K.O.
runtime running on the Mac. N.E.K.O. owns the long-term memory, persona,
proactive chat and plugins; the speaker is only a window onto it.

The host is standard-library only, so the WebSocket client here is a small
RFC 6455 implementation rather than a dependency. It is deliberately
**full-duplex** from day one: a server message may be the answer to a turn
we asked for, or an unsolicited/proactive message while no turn is running.
Only the former belongs to ``run()``; the latter is parked for a later stage
instead of leaking into the next answer.

Protocol (N.E.K.O. main server, default ws://127.0.0.1:48911), verified
against the real Mac client build 0.9.1 on 2026-11-09:
    -> {"action":"stream_data","input_type":"text","data":"...",
        "request_id":"<rid>"}
    <- {"type":"gemini_response","text":"...","request_id":"<rid>"}  deltas
    <- {"type":"system","data":"turn end","request_id":"<rid>"}

There is ONE session lease per character. ``start_session`` claims it and
broadcasts ``session_ended_by_server`` to the other windows, hiding their
text input. The desktop client sends plain text as ``stream_data`` without
claiming, so the bridge reuses the desktop session (see ``_session_active``)
and only sends ``start_session`` when no session exists at all. This is what
lets the Mac UI and the speaker stay interactive at the same time.
When we do claim one (standalone use, no desktop session):
    -> {"action":"start_session","input_type":"text","new_session":false,
        "request_id":"..."}
    <- {"type":"session_preparing", ...}   (ignored)
    <- {"type":"session_started","input_mode":"text","request_id":"..."}
then the same stream_data exchange above.
Every answer/turn-end frame echoes the request_id we sent. The server also
pushes proactive and unrelated messages on the same socket, so a frame whose
request_id does not match the running turn is parked instead of being joined
into the answer. A frame with no request_id is tolerated (older builds).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

from backends.base import Backend, BackendError, report
from backends.neko_relay import NekoRelayBackend

DEFAULT_URL = "ws://127.0.0.1:48911"
# "auto" follows the character the desktop client currently has selected
# (GET /api/characters/current_catgirl). Character names are case-sensitive:
# the Mac client's real one is "YUI", not "Yui" -- the memory directory is
# literally ~/Library/Application Support/N.E.K.O/memory/YUI.
DEFAULT_CHARACTER = "auto"
FALLBACK_CHARACTER = "YUI"
DEFAULT_TIMEOUT = 60.0
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_TEXT_KEYS = ("text", "content", "delta", "message", "response", "value")


def encode_frame(payload, opcode=0x1, fin=True, mask=True):
    """Build one RFC 6455 frame. Clients must mask."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    head = bytearray([first])
    flag = 0x80 if mask else 0
    if length < 126:
        head.append(flag | length)
    elif length < 65536:
        head.append(flag | 126)
        head.extend(struct.pack(">H", length))
    else:
        head.append(flag | 127)
        head.extend(struct.pack(">Q", length))
    if not mask:
        return bytes(head) + payload
    key = os.urandom(4)
    head.extend(key)
    body = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    return bytes(head) + body


def decode_frame(first_two, read_exact):
    """Decode one frame header/body read from a socket.

    ``read_exact(n)`` must return exactly n bytes or raise.
    Returns ``(fin, opcode, payload)``; control frames are returned as-is.
    """
    b1, b2 = first_two[0], first_two[1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", read_exact(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", read_exact(8))[0]
    key = read_exact(4) if masked else None
    payload = read_exact(length) if length else b""
    if key:
        payload = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    return fin, opcode, payload


class WsClient:
    """Minimal, standard-library WebSocket client (text frames only)."""

    def __init__(self, url, on_message, on_close=None, connect_timeout=3.0):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise BackendError("猫娘地址必须是 ws:// 或 wss://：" + str(url))
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = (parsed.path or "/").rstrip("/")
        self.secure = parsed.scheme == "wss"
        self.on_message = on_message
        self.on_close = on_close
        self.connect_timeout = connect_timeout
        self.sock = None
        self._buf = b""
        self._send_lock = threading.Lock()
        self._running = False
        self._reader = None

    @property
    def connected(self):
        return self._running and self.sock is not None

    def connect(self):
        if self.secure:
            import ssl
            raw = socket.create_connection((self.host, self.port),
                                           timeout=self.connect_timeout)
            self.sock = ssl.create_default_context().wrap_socket(
                raw, server_hostname=self.host)
        else:
            self.sock = socket.create_connection((self.host, self.port),
                                                 timeout=self.connect_timeout)
        self.sock.settimeout(self.connect_timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET " + self.path + " HTTP/1.1\r\n"
            "Host: " + self.host + ":" + str(self.port) + "\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: " + key + "\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        header, self._buf = self._read_headers()
        if "101" not in header.split("\r\n", 1)[0]:
            raise BackendError("猫娘握手失败：" + header.split("\r\n", 1)[0])
        expected = base64.b64encode(
            hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
        if expected.lower() not in header.lower():
            raise BackendError("猫娘握手校验失败（不是 WebSocket 服务）")
        # A short read timeout lets close() stop the reader thread promptly.
        self.sock.settimeout(1.0)
        self._running = True
        self._reader = threading.Thread(target=self._loop, name="neko-ws",
                                        daemon=True)
        self._reader.start()
        return self

    def _read_headers(self):
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BackendError("猫娘服务在握手中关闭了连接")
            data.extend(chunk)
            if len(data) > 65536:
                raise BackendError("猫娘握手响应过大")
        head, _, rest = bytes(data).partition(b"\r\n\r\n")
        return head.decode("latin-1"), rest

    def _read_exact(self, count):
        while len(self._buf) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:count], self._buf[count:]
        return out

    def _send_bytes(self, payload, opcode=0x1):
        with self._send_lock:
            if self.sock is None:
                raise BackendError("猫娘连接已断开")
            self.sock.sendall(encode_frame(payload, opcode=opcode))

    def send_json(self, obj):
        self._send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _loop(self):
        fragments = bytearray()
        while self._running:
            try:
                fin, opcode, payload = decode_frame(self._read_exact(2),
                                                    self._read_exact)
            except socket.timeout:
                continue
            except Exception:
                break
            if opcode == 0x8:          # close
                break
            if opcode == 0x9:          # ping -> pong
                try:
                    self._send_bytes(payload, opcode=0xA)
                except Exception:
                    break
                continue
            if opcode == 0xA:          # pong
                continue
            if opcode == 0x2:          # binary audio: phase 2 handles it
                continue
            if opcode == 0x1:
                fragments = bytearray(payload)
            elif opcode == 0x0:
                fragments.extend(payload)
            else:
                continue
            if not fin:
                continue
            try:
                message = json.loads(fragments.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                fragments = bytearray()
                continue
            fragments = bytearray()
            if isinstance(message, dict):
                try:
                    self.on_message(message)
                except Exception:
                    pass
        self._running = False
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        if self.on_close:
            try:
                self.on_close(self)
            except Exception:
                pass

    def close(self):
        self._running = False
        if self.sock is not None:
            try:
                self._send_bytes(b"", opcode=0x8)
            except Exception:
                pass
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None


def _belongs_to(message, pending):
    """True unless the frame explicitly names a different request.

    The real server echoes the request_id on answer and turn-end frames, and
    also pushes proactive/other-turn messages on the same socket. Frames
    without a request_id are tolerated for older builds.
    """
    rid = str(message.get("request_id") or "")
    return not rid or rid == str(pending["rid"])


def _frame_belongs(message, pending):
    """True when a frame may join the running turn.

    Reusing the desktop session means its broadcasts (proactive chat, the
    Mac user's own turns) share our socket. Those have no request_id or a
    foreign one, so in reuse mode only frames echoing OUR request_id count.
    Standalone (we claimed the session) keeps the older-build tolerance for
    frames without a request_id.
    """
    if pending is None or not pending.get("sent"):
        return False
    rid = str(message.get("request_id") or "")
    if pending.get("strict") and not rid:
        return False
    return _belongs_to(message, pending)


def _text_of(message):
    """Pull a text delta out of the several shapes N.E.K.O. has used."""
    for key in _TEXT_KEYS:
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for inner in _TEXT_KEYS:
                nested = value.get(inner)
                if isinstance(nested, str) and nested:
                    return nested
    return ""


class NekoBackend(Backend):
    """One assistant slot backed by a long-lived N.E.K.O. WebSocket."""

    id = "neko"
    name = "猫娘"

    def __init__(self, settings=None, state_store=None):
        settings = settings or {}
        self.url = str(settings.get("url") or DEFAULT_URL).rstrip("/")
        self._character_setting = str(settings.get("character")
                                      or DEFAULT_CHARACTER)
        self.character = (FALLBACK_CHARACTER
                          if self._character_setting.lower() == "auto"
                          else self._character_setting)
        try:
            self.timeout = float(settings.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT
        self._lock = threading.RLock()
        self._ws = None
        self._session_ready = threading.Event()
        self._session_error = None
        # True only when this connection sent start_session. A session ack
        # is broadcast to every window, so an ack/failure belonging to the
        # desktop client must not touch our readiness flags.
        self._claim_sent = False
        self._pending = None
        self._progress_cb = None
        # Unsolicited server messages (proactive chat, phase 3). Bounded so
        # a chatty companion cannot grow the host without limit.
        self.proactive = []
        # Relay mode: the desktop client owns the character's single socket,
        # so this backend must not open one. All turns go through the local
        # rokid_bridge plugin instead. Kept as a switch so the old direct
        # WebSocket path stays one config edit away.
        relay = settings.get("relay") or {}
        self._relay = (NekoRelayBackend(settings, state_store)
                       if isinstance(relay, dict) and relay.get("enabled")
                       else None)

    # -- connection -----------------------------------------------------------

    def _socket_url(self):
        return self.url + "/ws/" + urllib.parse.quote(self.character, safe="")

    def _http_base(self):
        return self.url.replace("wss://", "https://").replace("ws://", "http://")

    def _detect_character(self):
        """Ask the running N.E.K.O. which character the desktop UI is using."""
        try:
            with urllib.request.urlopen(
                    self._http_base() + "/api/characters/current_catgirl",
                    timeout=1.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            name = payload.get("current_catgirl") or payload.get("name") or ""
            if isinstance(name, str) and name.strip():
                return name.strip()
        except Exception:
            pass
        return FALLBACK_CHARACTER

    def _session_active(self):
        """Is the desktop client already holding this character's session?

        N.E.K.O. has ONE session lease per character. ``start_session`` tears
        that session down and broadcasts ``session_ended_by_server`` to the
        other windows, which hides their text input. The official desktop
        client sends plain text as ``stream_data`` without ever claiming a
        session, so the bridge must do the same while the desktop owns one.

        An unknown/error status is treated as "active": never risk stealing
        the desktop UI's lease just because a status probe failed.
        """
        try:
            url = (self._http_base() + "/api/characters/catgirl/"
                   + urllib.parse.quote(self.character, safe="")
                   + "/voice_mode_status")
            with urllib.request.urlopen(url, timeout=1.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return bool(payload.get("is_active"))
        except Exception:
            return True

    def _ensure(self):
        with self._lock:
            if self._ws is not None and self._ws.connected and self._session_ready.is_set():
                return
            if self._character_setting.lower() == "auto":
                self.character = self._detect_character()
            if self._ws is not None:
                self._ws.close()
                self._ws = None
            self._session_ready.clear()
            self._session_error = None
            self._claim_sent = False
            try:
                client = WsClient(self._socket_url(), self._on_message,
                                  self._on_close).connect()
            except BackendError:
                raise
            except OSError as error:
                raise BackendError(
                    "猫娘还没启动：请先在 Mac 上运行 N.E.K.O.（" + self.url
                    + "，" + str(error) + "）")
            self._ws = client
            if self._session_active():
                # Reuse the desktop session. Sending start_session here would
                # kick the Mac UI out of text input (see _session_active).
                self._session_ready.set()
            else:
                # Nobody is holding the lease (e.g. bridge used standalone).
                # Claim one so stream_data has a session to land in.
                self._claim_sent = True
                client.send_json({"action": "start_session",
                                  "input_type": "text",
                                  "new_session": False,
                                  "request_id": "neko-start-"
                                                + uuid.uuid4().hex[:12]})
        if not self._session_ready.wait(min(self.timeout, 15.0)):
            raise BackendError("猫娘会话启动超时（没有收到 session_started）")
        if self._session_error:
            raise BackendError("猫娘会话启动失败：" + str(self._session_error))

    def _on_close(self, client=None):
        with self._lock:
            # A reconnect can already have installed a newer client; that
            # one's session must not be invalidated by the old one's exit.
            current = client is None or self._ws is client
            pending = self._pending
            if current:
                self._session_ready.clear()
                # Drop the stale client. Without this, _ensure() can mistake
                # a half-dead socket for a live one: the real server closes
                # idle sessions and only the next send discovers it.
                self._ws = None
        if current and pending is not None:
            self._fail(pending, "猫娘连接断开")

    # -- duplex dispatch ------------------------------------------------------

    def _on_message(self, message):
        kind = str(message.get("type") or message.get("action") or "")
        if kind == "session_started":
            # Broadcast to every window. Only our own claim may settle us.
            if self._claim_sent:
                self._session_ready.set()
            return
        if kind == "session_failed":
            # Another window's failed start must not poison a reused session.
            if self._claim_sent:
                self._session_error = _text_of(message) or "session_failed"
                self._session_ready.set()
            return
        if kind in ("session_ended_by_server", "catgirl_switched", "reload_page"):
            # Another client (the desktop UI) claimed this character's single
            # connection. The next turn reconnects; memory lives on the server.
            with self._lock:
                self._session_ready.clear()
            return
        if kind == "gemini_response":
            piece = _text_of(message)
            if not piece:
                return
            with self._lock:
                pending = self._pending
                callback = self._progress_cb
            # Only frames belonging to our own stream_data may join the
            # answer; the desktop session broadcasts other turns as well.
            if _frame_belongs(message, pending):
                pending["chunks"].append(piece)
                if pending["first"]:
                    pending["first"] = False
                    report(callback, "speaking", "猫娘在说话")
            else:
                self.proactive.append({"at": time.time(), "text": piece,
                                       "request_id": str(message.get(
                                           "request_id") or "")})
                del self.proactive[:-32]
            return
        if kind == "system" and "turn end" in str(message).lower():
            with self._lock:
                pending = self._pending
            if _frame_belongs(message, pending):
                pending["event"].set()
            return
        if kind in ("expression", "focus_state", "focus_charge"):
            with self._lock:
                callback = self._progress_cb
            report(callback, "speaking", "")
            return
        if kind in ("agent_status_update", "agent_task_update", "agent_notification"):
            with self._lock:
                callback = self._progress_cb
            report(callback, "working", "猫娘在做事")
            return
        if kind == "status":
            code = str(message.get("code") or message.get("error") or "")
            if (code and "error" in code.lower()) or message.get("status") == "error":
                with self._lock:
                    pending = self._pending
                if pending is not None:
                    self._fail(pending, _text_of(message) or code)

    def _fail(self, pending, detail):
        pending["error"] = str(detail)
        pending["event"].set()

    # -- Backend contract -----------------------------------------------------

    def run(self, text, request_id, on_progress=None, memory_text=None):
        """Answer one turn, retrying once if an idle socket was dropped.

        N.E.K.O. closes idle sessions. Memory lives in the Memory Server, so
        a fresh socket loses nothing; a turn that never reached the model is
        safe to resend exactly once.

        ``memory_text`` replaces what the server stores as the user's words
        while the model still sees ``text``. Host-generated prompts use it so
        the         instruction itself never becomes a remembered "user utterance".
        """
        if self._relay is not None:
            return self._relay.run(text, request_id, on_progress, memory_text)
        for attempt in (0, 1):
            try:
                return self._run_once(text, request_id, on_progress,
                                      memory_text)
            except BackendError as error:
                detail = str(error)
                reconnectable = any(mark in detail for mark in (
                    "连接断开", "消息发送失败", "会话启动超时"))
                if attempt or not reconnectable:
                    raise
                with self._lock:
                    self._ws = None
                    self._session_ready.clear()
        raise BackendError("猫娘连接不稳定")

    def _run_once(self, text, request_id, on_progress, memory_text=None):
        self._ensure()
        report(on_progress, "thinking", "猫娘在想")
        with self._lock:
            strict_rid = not self._claim_sent
            client = self._ws
        pending = {"rid": request_id, "chunks": [], "event": threading.Event(),
                   "error": None, "first": True, "cancelled": False,
                   "sent": False, "strict": strict_rid}
        with self._lock:
            self._pending = pending
            self._progress_cb = on_progress
        if client is None:
            raise BackendError("猫娘连接断开")
        try:
            payload = {"action": "stream_data", "input_type": "text",
                       "data": str(text),
                       "request_id": str(request_id)}
            if memory_text:
                payload["memory_text"] = str(memory_text)
            # Hold the engine lock across send + flag so the reader thread
            # cannot see a reply before "sent" is set (it reads pending under
            # the same lock), while an earlier broadcast still stays parked.
            with self._lock:
                client.send_json(payload)
                pending["sent"] = True
        except Exception as error:
            with self._lock:
                self._pending = None
                self._progress_cb = None
                self._ws = None
            raise BackendError("猫娘消息发送失败：" + str(error))
        finished = pending["event"].wait(self.timeout)
        with self._lock:
            if self._pending is pending:
                self._pending = None
                self._progress_cb = None
        if pending["cancelled"]:
            return ""
        if pending["error"]:
            raise BackendError(str(pending["error"]))
        if not finished:
            raise BackendError("猫娘在 " + ("%g" % self.timeout) + " 秒内没有回答")
        answer = "".join(pending["chunks"]).strip()
        if not answer:
            raise BackendError("猫娘没有回复内容")
        return answer

    def cancel(self, request_id):
        if self._relay is not None:
            self._relay.cancel(request_id)
            return
        with self._lock:
            pending = self._pending
            if pending is None or pending["rid"] != request_id:
                return
            pending["cancelled"] = True
        pending["event"].set()

    def health(self):
        if self._relay is not None:
            return self._relay.health()
        try:
            with urllib.request.urlopen(self._http_base() + "/health",
                                        timeout=1.5) as response:
                if response.status == 200:
                    return True, "ok"
                return False, "HTTP " + str(response.status)
        except Exception as error:
            return False, "N.E.K.O. 未启动（" + str(error) + "）"

    def close(self):
        if self._relay is not None:
            self._relay.close()
        with self._lock:
            client, self._ws = self._ws, None
            self._session_ready.clear()
        if client is not None:
            client.close()
