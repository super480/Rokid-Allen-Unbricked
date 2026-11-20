"""猫娘 backend: WebSocket framing, duplex dispatch, failures and routing."""
import base64
import hashlib
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config  # noqa: E402
from backends import BACKENDS, load_backend  # noqa: E402
from backends.base import BackendError  # noqa: E402
from backends.neko import (NekoBackend, WsClient, _text_of,  # noqa: E402
                           decode_frame, encode_frame)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _memory_reader(raw):
    """A read_exact() for decode_frame over an in-memory frame."""
    cursor = [0]

    def read_exact(count):
        out = raw[cursor[0]:cursor[0] + count]
        if len(out) != count:
            raise AssertionError("short frame")
        cursor[0] += count
        return out

    return read_exact


class FrameTest(unittest.TestCase):
    def test_masked_text_roundtrip(self):
        raw = encode_frame("猫娘你好", mask=True)
        read = _memory_reader(raw)
        fin, opcode, payload = decode_frame(read(2), read)
        self.assertTrue(fin)
        self.assertEqual(opcode, 0x1)
        self.assertEqual(payload.decode("utf-8"), "猫娘你好")

    def test_extended_lengths(self):
        for size in (125, 126, 65535, 65536):
            raw = encode_frame(b"x" * size, mask=True)
            read = _memory_reader(raw)
            fin, opcode, payload = decode_frame(read(2), read)
            self.assertEqual(len(payload), size)
            self.assertEqual(opcode, 0x1)

    def test_server_frame_is_unmasked(self):
        raw = encode_frame('{"type":"session_started"}', mask=False)
        self.assertEqual(raw[1] & 0x80, 0)
        read = _memory_reader(raw)
        _, _, payload = decode_frame(read(2), read)
        self.assertEqual(json.loads(payload)["type"], "session_started")

    def test_text_of_lenient_shapes(self):
        for message in ({"text": "a"}, {"content": "b"}, {"delta": "c"},
                        {"message": {"content": "d"}}, {"response": "e"}):
            self.assertTrue(_text_of(message))
        self.assertEqual(_text_of({"type": "ping"}), "")


class MockNeko:
    """A tiny N.E.K.O.-shaped server: HTTP /health + one WebSocket."""

    def __init__(self, mode="ok", proactive=False, port=0,
                 session_active=True):
        self.mode = mode
        self.proactive = proactive
        # The real server exposes /voice_mode_status; is_active tells the
        # bridge whether the desktop client already holds the session lease.
        self.session_active = session_active
        self.current_character = "YUI"
        self.last_start = None
        self.last_stream = None
        self.stream_count = 0
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", port))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.url = "ws://127.0.0.1:" + str(self.port)
        self.alive = True
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while self.alive:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(5)
        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head, _, buf = buf.partition(b"\r\n\r\n")
            text = head.decode("latin-1")
            if "upgrade: websocket" not in text.lower():
                first_line = text.split("\r\n", 1)[0]
                if "/api/characters/current_catgirl" in first_line:
                    body = json.dumps(
                        {"current_catgirl": self.current_character}).encode()
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode()
                        + b"\r\nConnection: close\r\n\r\n" + body)
                elif "/voice_mode_status" in first_line:
                    body = json.dumps(
                        {"is_active": self.session_active,
                         "is_voice_mode": False}).encode()
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode()
                        + b"\r\nConnection: close\r\n\r\n" + body)
                else:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                                 b"Connection: close\r\n\r\nok")
                return
            key = ""
            for line in text.split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            accept = base64.b64encode(
                hashlib.sha1((key + GUID).encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                          "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Accept: " + accept + "\r\n\r\n"
                          ).encode("latin-1"))

            def read_exact(count):
                nonlocal buf
                while len(buf) < count:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise ConnectionError("closed")
                    buf += chunk
                out, buf = buf[:count], buf[count:]
                return out

            def send(obj):
                conn.sendall(encode_frame(
                    json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                    mask=False))

            if self.proactive:
                # Before the session starts, so it can never be mistaken
                # for the answer to a turn we asked for.
                send({"type": "gemini_response", "text": "主动搭话"})
                time.sleep(0.2)
            while True:
                fin, opcode, payload = decode_frame(read_exact(2), read_exact)
                if opcode == 0x8:
                    return
                if not fin or opcode != 0x1:
                    continue
                message = json.loads(payload.decode("utf-8"))
                if message.get("action") == "start_session":
                    self.last_start = message
                    # The real server only acks a claim after receiving it.
                    # A reused-session client never sees these frames.
                    if self.mode == "failed":
                        send({"type": "session_failed",
                              "message": "no provider"})
                    else:
                        send({"type": "session_started",
                              "input_mode": "text"})
                    continue
                if message.get("action") != "stream_data":
                    continue
                rid = message.get("request_id")
                self.last_stream = message
                self.stream_count += 1
                if self.mode == "drop_first" and self.stream_count == 1:
                    # The real server silently closes an idle session. The
                    # client must notice, reconnect and resend this turn.
                    return
                if self.mode == "delay":
                    time.sleep(1.5)
                if self.mode == "silent":
                    continue
                if self.mode == "foreign":
                    # Frames belonging to somebody else's turn must not end
                    # ours or be joined into our answer.
                    send({"type": "gemini_response", "text": "别人的消息",
                          "request_id": "someone-else"})
                    send({"type": "system", "data": "turn end",
                          "request_id": "someone-else"})
                if self.mode == "empty":
                    send({"type": "system", "data": "turn end",
                          "request_id": rid})
                    continue
                send({"type": "gemini_response", "text": "你好呀，",
                      "request_id": rid})
                send({"type": "gemini_response", "content": "我是猫娘。",
                      "request_id": rid})
                send({"type": "system", "data": "turn end",
                      "request_id": rid})
        except Exception:
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def close(self):
        self.alive = False
        try:
            self.listener.close()
        except Exception:
            pass


def backend_for(server, **settings):
    return NekoBackend(dict({"url": server.url, "timeout": 3},
                            **settings))


class NekoBackendTest(unittest.TestCase):
    def test_answer_and_progress(self):
        server = MockNeko()
        backend = backend_for(server)
        states = []
        try:
            answer = backend.run("你好", "r1",
                                 lambda state, detail="": states.append(state))
        finally:
            backend.close()
            server.close()
        self.assertEqual(answer, "你好呀，我是猫娘。")
        self.assertIn("thinking", states)
        self.assertIn("speaking", states)

    def test_health(self):
        server = MockNeko()
        try:
            ok, detail = backend_for(server).health()
            self.assertTrue(ok, detail)
        finally:
            server.close()

    def test_not_running_is_friendly(self):
        backend = NekoBackend({"url": "ws://127.0.0.1:1", "timeout": 2})
        ok, detail = backend.health()
        self.assertFalse(ok)
        self.assertIn("未启动", detail)
        with self.assertRaises(BackendError) as caught:
            backend.run("你好", "r1")
        self.assertIn("还没启动", str(caught.exception))
        backend.close()

    def test_session_failure(self):
        # Only a session we claimed ourselves can fail to start.
        server = MockNeko(mode="failed", session_active=False)
        backend = backend_for(server)
        try:
            with self.assertRaises(BackendError) as caught:
                backend.run("你好", "r1")
            self.assertIn("会话启动失败", str(caught.exception))
        finally:
            backend.close()
            server.close()

    def test_empty_answer(self):
        server = MockNeko(mode="empty")
        backend = backend_for(server)
        try:
            with self.assertRaises(BackendError) as caught:
                backend.run("你好", "r1")
            self.assertIn("没有回复", str(caught.exception))
        finally:
            backend.close()
            server.close()

    def test_timeout(self):
        server = MockNeko(mode="silent")
        backend = backend_for(server, timeout=0.5)
        try:
            with self.assertRaises(BackendError) as caught:
                backend.run("你好", "r1")
            self.assertIn("没有回答", str(caught.exception))
        finally:
            backend.close()
            server.close()

    def test_cancel_returns_without_error(self):
        server = MockNeko(mode="delay")
        backend = backend_for(server, timeout=5)
        try:
            threading.Timer(0.2, backend.cancel, args=("r1",)).start()
            self.assertEqual(backend.run("你好", "r1"), "")
        finally:
            backend.close()
            server.close()

    def test_unsolicited_message_does_not_leak(self):
        server = MockNeko(proactive=True)
        backend = backend_for(server)
        try:
            answer = backend.run("你好", "r1")
            self.assertEqual(answer, "你好呀，我是猫娘。")
            self.assertNotIn("主动搭话", answer)
            self.assertTrue(any("主动搭话" in item["text"]
                                for item in backend.proactive))
            # The parked message must not appear in the next turn either.
            self.assertEqual(backend.run("再说一次", "r2"),
                             "你好呀，我是猫娘。")
        finally:
            backend.close()
            server.close()

    def test_foreign_request_id_is_not_joined_nor_ends_turn(self):
        server = MockNeko(mode="foreign")
        backend = backend_for(server)
        try:
            answer = backend.run("你好", "r1")
            self.assertEqual(answer, "你好呀，我是猫娘。")
            self.assertNotIn("别人的消息", answer)
            self.assertTrue(any("别人的消息" in item["text"]
                                for item in backend.proactive))
        finally:
            backend.close()
            server.close()

    def test_claims_session_only_when_the_desktop_has_none(self):
        server = MockNeko(session_active=False)
        backend = backend_for(server)
        try:
            backend.run("你好", "r1")
            start = server.last_start
            self.assertEqual(start["action"], "start_session")
            self.assertEqual(start["input_type"], "text")
            self.assertFalse(start["new_session"])
            self.assertTrue(start["request_id"])
        finally:
            backend.close()
            server.close()

    def test_reuses_desktop_session_without_start_session(self):
        """The Mac UI must keep its text input while the speaker talks."""
        server = MockNeko(session_active=True)
        backend = backend_for(server)
        try:
            answer = backend.run("你好", "r1")
            self.assertEqual(answer, "你好呀，我是猫娘。")
            self.assertIsNone(server.last_start)
            self.assertEqual(server.last_stream["data"], "你好")
            self.assertTrue(backend._session_ready.is_set())
        finally:
            backend.close()
            server.close()

    def test_a_broadcast_session_failure_does_not_poison_a_reused_session(self):
        backend = NekoBackend({"url": "ws://127.0.0.1:1", "timeout": 2})
        try:
            backend._claim_sent = False
            backend._session_ready.set()
            backend._on_message({"type": "session_failed",
                                 "message": "another window"})
            self.assertIsNone(backend._session_error)
            self.assertTrue(backend._session_ready.is_set())
        finally:
            backend.close()

    def test_an_unreachable_status_probe_defaults_to_reuse(self):
        backend = NekoBackend({"url": "ws://127.0.0.1:1", "timeout": 2})
        # Never steal the desktop lease just because a probe failed.
        self.assertTrue(backend._session_active())
        backend.close()

    def test_session_active_reads_the_real_status_shape(self):
        import io
        from unittest import mock

        backend = NekoBackend({"url": "ws://127.0.0.1:9999", "timeout": 2})
        payload = b'{"is_active":false,"is_voice_mode":false}'
        with mock.patch("backends.neko.urllib.request.urlopen",
                        return_value=io.BytesIO(payload)) as probe:
            self.assertFalse(backend._session_active())
        url = probe.call_args[0][0]
        self.assertIn("/api/characters/catgirl/YUI/voice_mode_status", url)
        backend.close()

    def test_memory_text_is_sent_only_when_requested(self):
        server = MockNeko()
        backend = backend_for(server)
        try:
            backend.run("你好", "r1")
            first = dict(server.last_stream)
            self.assertEqual(first["data"], "你好")
            self.assertNotIn("memory_text", first)

            backend.run("（系统请求）请说一句", "r2",
                        memory_text="嗯。")
            second = dict(server.last_stream)
            self.assertEqual(second["data"], "（系统请求）请说一句")
            self.assertEqual(second["memory_text"], "嗯。")
        finally:
            backend.close()
            server.close()

    def test_auto_character_follows_running_client(self):
        server = MockNeko()
        server.current_character = "MIAO"
        backend = backend_for(server, character="auto")
        try:
            backend.run("你好", "r1")
            self.assertEqual(backend.character, "MIAO")
            self.assertEqual(backend._ws.path, "/ws/MIAO")
        finally:
            backend.close()
            server.close()

    def test_dropped_idle_connection_is_retried_once(self):
        server = MockNeko(mode="drop_first")
        backend = backend_for(server, timeout=5)
        try:
            answer = backend.run("你好", "r1")
            self.assertEqual(answer, "你好呀，我是猫娘。")
            self.assertEqual(server.stream_count, 2)
        finally:
            backend.close()
            server.close()

    def test_ws_client_rejects_bad_scheme(self):
        with self.assertRaises(BackendError):
            WsClient("http://127.0.0.1:1", lambda message: None)


class RegistryTest(unittest.TestCase):
    def test_fifth_assistant_is_registered(self):
        self.assertIn("neko", config.REGISTRY)
        item = config.REGISTRY["neko"]
        self.assertEqual(item["name"], "猫娘")
        self.assertEqual(item["backend"], "neko")
        self.assertTrue(item["enabled"])

    def test_backend_loads(self):
        self.assertEqual(BACKENDS["neko"][1], "NekoBackend")
        self.assertIsInstance(load_backend("neko", {}, None), NekoBackend)

    def test_wake_word_default(self):
        row = config.DEFAULTS["wake"]["agents"]["neko"]
        self.assertEqual(row["pinyin"], "mao1niang2")
        self.assertIn("毛娘", row["aliases"])
        # No bare common characters in the alias table.
        self.assertTrue(all(len(alias) > 1 for alias in row["aliases"]))

    def test_neko_settings_default(self):
        self.assertEqual(config.DEFAULTS["neko"]["url"],
                         "ws://127.0.0.1:48911")
        # Case-sensitive names are read from the running client at connect.
        self.assertEqual(config.DEFAULTS["neko"]["character"], "auto")
        self.assertIn("neko", config.BACKEND_SECTIONS)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="N.E.K.O. backend checks")
    parser.add_argument("--serve", type=int, default=0, metavar="PORT",
                        help="run a local mock N.E.K.O. server instead of tests")
    args = parser.parse_args()
    if args.serve:
        mock = MockNeko(port=args.serve)
        print("mock N.E.K.O. listening on " + mock.url, flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            mock.close()
    else:
        unittest.main()
