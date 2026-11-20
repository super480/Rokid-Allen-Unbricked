"""Relay mode: the desktop client owns the only WebSocket.

No real HTTP, no WebSocket and no audio here. The relay's loopback calls are
patched, and the Mac speaker uses a fake runner, so these tests verify the
wiring without ever making a sound or touching the N.E.K.O. app.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
import mac_speech  # noqa: E402
from backends.neko import NekoBackend  # noqa: E402
from backends.neko_relay import NekoRelayBackend  # noqa: E402
from engine import Engine  # noqa: E402


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self):
        class Process:
            def __init__(self):
                self.stdin = self
                self.data = b""

            def write(self, chunk):
                self.data += chunk

            def close(self):
                pass

            def wait(self):
                return 0

            def terminate(self):
                pass

        p = Process()
        self.calls.append(p)
        return p


def make_engine(root):
    config = config_module.create_config(root / "config.json")
    config["_root"] = str(root)
    config["_path"] = str(root / "config.json")
    config["roon_bridge_url"] = "http://127.0.0.1:1"
    return config, Engine(config, state_file=str(root / "state.json"))


class RelayBackendTest(unittest.TestCase):
    def test_relay_off_by_default(self):
        backend = NekoBackend({"relay": {"enabled": False}})
        self.assertIsNone(backend._relay)

    def test_relay_on_installs_a_backend_without_a_socket(self):
        backend = NekoBackend({"relay": {"enabled": True,
                                         "url": "http://127.0.0.1:48917"}})
        self.assertIsInstance(backend._relay, NekoRelayBackend)
        self.assertIsNone(backend._ws)

    def test_relay_run_never_calls_ensure(self):
        backend = NekoBackend({"relay": {"enabled": True}})
        answers = [
            {"fingerprint": "old", "text": "上一句"},
            {"fingerprint": "new", "text": "这是一条测试回复。"},
        ]
        with mock.patch.object(backend._relay, "_request",
                               side_effect=[answers[0], {"ok": True},
                                            answers[1]]) as request, \
                mock.patch.object(backend, "_ensure",
                                  side_effect=AssertionError("opened a WebSocket")):
            self.assertEqual(backend.run("你好", "rid-1"), "这是一条测试回复。")
        self.assertEqual(request.call_count, 3)
        self.assertIsNone(backend._ws)

    def test_relay_does_not_reuse_a_stale_reply(self):
        backend = NekoBackend({"relay": {"enabled": True}})
        same = {"fingerprint": "same", "text": "旧回复"}

        def fake_request(path, payload=None, timeout=5.0):
            return {"ok": True} if path == "/input" else same

        clock = iter([0.0, 0.0, 100.0])
        with mock.patch.object(backend._relay, "_request",
                               side_effect=fake_request), \
                mock.patch("backends.neko_relay.time.monotonic",
                           side_effect=lambda: next(clock, 100.0)), \
                mock.patch("backends.neko_relay.time.sleep"):
            with self.assertRaises(Exception) as ctx:
                backend.run("你好", "rid-2")
            self.assertIn("没有回答", str(ctx.exception))

    def test_relay_cancel_stops_waiting(self):
        relay = NekoRelayBackend({"relay": {"enabled": True}})
        relay.cancel("rid-3")
        with mock.patch.object(relay, "_request",
                               return_value={"fingerprint": "", "text": ""}):
            self.assertEqual(relay.run("你好", "rid-3"), "")

    def test_relay_health_reports_the_plugin(self):
        relay = NekoRelayBackend({"relay": {"enabled": True}})
        with mock.patch.object(relay, "_request",
                               return_value={"ok": True}):
            self.assertEqual(relay.health(), (True, "ok"))
        # Go through the real _request so its OSError -> BackendError
        # translation is covered too.
        with mock.patch("backends.neko_relay.urllib.request.urlopen",
                        side_effect=OSError("refused")):
            ok, detail = relay.health()
            self.assertFalse(ok)
            self.assertIn("没响应", detail)


class RelayEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config, self.engine = make_engine(self.root)
        self.runner = FakeRunner()
        self.engine.mac_speaker = mac_speech.MacSpeech(runner=self.runner)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def of_type(self, kind):
        return [c for c in self.engine.pending(0) if c["type"] == kind]

    def test_the_switch_persists_and_rebuilds_the_backend(self):
        self.assertFalse(self.engine.neko_relay_settings()["enabled"])
        self.engine.backend_for("neko")  # cached with relay off
        self.assertIn("neko", self.engine.backends)
        result = self.engine.set_neko_relay(enabled=True)
        self.assertTrue(result["enabled"])
        self.assertNotIn("neko", self.engine.backends)  # next call rebuilds
        backend = self.engine.backend_for("neko")
        self.assertIsInstance(backend._relay, NekoRelayBackend)
        self.assertFalse(self.engine.set_neko_relay(enabled=False)["enabled"])
        self.assertIsNone(self.engine.backend_for("neko")._relay)

    def test_relay_speaker_is_silent_but_shows_the_subtitle(self):
        self.engine.set_neko_relay(enabled=True)
        self.engine.deliver_speech("今天天气不错，记得带伞。", "neko", "rid-4")
        self.assertEqual(self.runner.calls, [])       # no local `say`
        self.assertEqual(self.of_type("speak"), [])    # no device TTS
        display = self.of_type("display")
        self.assertEqual(len(display), 1)
        self.assertEqual(display[0]["agent"], "neko")

    def test_relay_opens_an_echo_window_for_the_desktop_voice(self):
        self.engine.set_neko_relay(enabled=True)
        self.engine.deliver_speech("这是一句会被 Mac 朗读出来的话。",
                                  "neko", "rid-5")
        self.assertTrue(self.engine.wake_during_speech())
        entry = self.engine.recent_speeches[-1]
        self.assertTrue(entry.get("mac"))
        self.assertIsNone(entry.get("ended"))  # expires on cap, not a process
        self.assertGreater(entry["cap"], 1.5)
        # The desktop reads the original line; we cannot dodge its wording.
        self.assertIn("mac", entry["text"].lower())

    def test_relay_keeps_the_device_playback_report_from_closing_the_window(self):
        self.engine.set_neko_relay(enabled=True)
        self.engine.deliver_speech("这是一条测试回复。", "neko", "rid-6")
        entry = self.engine.recent_speeches[-1]
        self.engine.handle_event({"type": "playback_done"})
        self.assertIsNone(entry.get("ended"))

    def test_relay_stops_host_proactive_chat(self):
        self.engine.set_neko_relay(enabled=True)
        self.engine.selected = "neko"
        self.config["neko"]["proactive"]["enabled"] = True
        self.assertFalse(self.engine._proactive_gate())

    def test_relay_returns_avatar_to_idle_after_the_desktop_reads(self):
        # Relay mode never sends `speak`, so without a timer the device would
        # keep the "speaking" face forever. The scheduled reset must bring it
        # back to idle on the neko character.
        self.engine.set_neko_relay(enabled=True)
        self.engine.selected = "neko"
        self.engine.deliver_speech("这条回复由 Mac 朗读。", "neko", "rid-avatar")
        # No device TTS command, and a timer was queued.
        self.assertEqual(self.of_type("speak"), [])
        self.assertEqual(len(self.engine._avatar_idle_timers), 1)
        # Fire it directly (no real sleep): the avatar goes back to idle.
        with mock.patch("engine.threading.current_thread"), \
                mock.patch.object(self.engine, "neko_relay_settings",
                                  return_value={"enabled": True}):
            self.engine._avatar_idle_elapsed("rid-avatar")
        last = [c for c in self.engine.pending(0)
                if c["type"] == "display"][-1]
        self.assertEqual(last["state"], "idle")
        self.assertEqual(last["agent"], "neko")

    def test_relay_avatar_idle_skips_a_newer_turn(self):
        # A stale timer must not drag a turn that is still speaking back to
        # idle: the newer request owns the face until its own timer fires.
        self.engine.set_neko_relay(enabled=True)
        self.engine.selected = "neko"
        self.engine.active_request_id = "rid-new"
        before = len(self.of_type("display"))
        with mock.patch("engine.threading.current_thread"), \
                mock.patch.object(self.engine, "neko_relay_settings",
                                  return_value={"enabled": True}):
            self.engine._avatar_idle_elapsed("rid-old")
        self.assertEqual(len(self.of_type("display")), before)

    def test_relay_avatar_idle_skips_after_assistant_switch(self):
        self.engine.set_neko_relay(enabled=True)
        self.engine.selected = "hermes"
        before = len(self.of_type("display"))
        with mock.patch("engine.threading.current_thread"), \
                mock.patch.object(self.engine, "neko_relay_settings",
                                  return_value={"enabled": True}):
            self.engine._avatar_idle_elapsed("rid-old")
        self.assertEqual(len(self.of_type("display")), before)

    def test_non_relay_speaking_has_no_avatar_timer(self):
        # Normal mode resets uiState via the device's playback_done callback,
        # so it must not schedule the relay-only timer.
        self.engine.deliver_speech("若琪自己朗读。", "hermes", "rid-local")
        self.assertEqual(self.engine._avatar_idle_timers, set())

    def test_status_exposes_the_switch(self):
        self.engine.set_neko_relay(enabled=True)
        self.assertTrue(self.engine.status()["neko_relay"]["enabled"])


if __name__ == "__main__":
    unittest.main()
