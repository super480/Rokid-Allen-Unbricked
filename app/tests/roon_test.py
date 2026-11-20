"""Roon display + voice remote, with a fake local sidecar.

Nothing here opens a real Roon Core or plays audio: the Node extension is
replaced by a tiny HTTP server that answers /status, /control and /settings.
"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
import server as server_module  # noqa: E402
from engine import Engine  # noqa: E402
from roon import RoonClient  # noqa: E402

ZONE = {
    "zone_id": "zone-1", "display_name": "客厅 AirPlay",
    "state": "playing", "title": "晴天", "artist": "周杰伦",
    "album": "叶惠美", "seek_position": 42, "length": 269,
    "image_key": "img-1", "outputs": [{"output_id": "out-1",
                                       "display_name": "客厅",
                                       "volume": {"min": 0, "max": 100,
                                                  "step": 2, "value": 40}}]
}


def ready_status(zone=None):
    zone = zone or ZONE
    return {"ok": True, "online": True, "socket": True, "paired": True,
            "authorised": True, "stage": "ready",
            "core": {"core_id": "core-1", "display_name": "Super-Mac-mini",
                     "display_version": "2.0"},
            "zones": [zone], "zone": zone,
            "settings": {"host": "192.0.2.4", "port": 9330, "zone_id": ""},
            "updated_at": time.time(), "error": ""}


class FakeSidecar:
    def __init__(self, status=None):
        self.status = status if status is not None else ready_status()
        self.controls = []
        self.settings = []
        self.status_requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, payload, content_type="application/json"):
                body = (json.dumps(payload).encode("utf-8")
                        if content_type == "application/json" else payload)
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/status"):
                    outer.status_requests += 1
                    return self.reply(200, outer.status)
                if self.path.startswith("/image"):
                    return self.reply(200, b"\xff\xd8\xffFAKE" + b"\x00" * 64,
                                      "image/jpeg")
                return self.reply(404, {"ok": False})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path.startswith("/control"):
                    outer.controls.append(body)
                    return self.reply(200, {"ok": True})
                if self.path.startswith("/settings"):
                    outer.settings.append(body)
                    return self.reply(200, {"ok": True})
                return self.reply(404, {"ok": False})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.02},
                                       daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class RoonClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "roon-target.json"
        self.fake = FakeSidecar()

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def client(self, url=None):
        return RoonClient(base_url=url or self.fake.url,
                          state_path=self.state_path)

    def test_status_merges_the_sidecar_and_the_host_target(self):
        client = self.client()
        result = client.status()
        self.assertTrue(result["online"])
        self.assertEqual(result["stage"], "ready")
        self.assertEqual(result["target"], "auto")
        self.assertEqual(result["zone"]["title"], "晴天")

    def test_offline_is_quiet_and_never_raises(self):
        client = self.client("http://127.0.0.1:1")
        result = client.status()
        self.assertFalse(result["online"])
        self.assertEqual(result["stage"], "offline")
        self.assertEqual(result["target"], "auto")

    def test_now_playing_shape(self):
        np = RoonClient.now_playing(ready_status())
        self.assertEqual(np["title"], "晴天")
        self.assertEqual(np["artist"], "周杰伦")
        self.assertEqual(np["zone_name"], "客厅 AirPlay")
        self.assertEqual(np["state"], "playing")
        self.assertEqual(np["seek_position"], 42)
        self.assertIsNone(RoonClient.now_playing({"zone": None}))

    def test_control_forwards_action_and_selected_zone(self):
        client = self.client()
        client.configure(zone_id="zone-1")
        result = client.control("next")
        self.assertTrue(result["ok"])
        self.assertEqual(self.fake.controls[-1]["action"], "next")
        self.assertEqual(self.fake.controls[-1]["zone_id"], "zone-1")

    def test_volume_action_keeps_its_value(self):
        client = self.client()
        client.configure(target="roon")
        client.control("volume_set", value=55)
        self.assertEqual(self.fake.controls[-1]["action"], "volume_set")
        self.assertEqual(self.fake.controls[-1]["value"], 55)

    def test_image_returns_bytes(self):
        pair = self.client().image("img-1")
        self.assertIsNotNone(pair)
        self.assertEqual(pair[0][:3], b"\xff\xd8\xff")
        self.assertIsNone(self.client().image(""))

    def test_settings_persist_across_restarts(self):
        first = self.client()
        first.configure(target="roon", zone_id="zone-1")
        self.assertEqual(json.loads(self.state_path.read_text())["target"],
                         "roon")
        second = self.client()
        self.assertEqual(second.target, "roon")
        self.assertEqual(second.zone_id, "zone-1")

    def test_auto_routing_respects_the_local_player(self):
        client = self.client()
        client.status()
        self.assertTrue(client.routed_to_roon("idle"))
        self.assertFalse(client.routed_to_roon("playing"))
        self.assertFalse(client.routed_to_roon("paused"))
        client.configure(target="local")
        self.assertFalse(client.routed_to_roon("idle"))
        client.configure(target="roon")
        self.assertTrue(client.routed_to_roon("idle"))

    def test_no_routing_while_waiting_for_authorisation(self):
        self.fake.status = {"online": True, "stage": "waiting_authorisation",
                            "zones": [], "zone": None, "socket": True}
        client = self.client()
        client.status()
        self.assertFalse(client.routed_to_roon("idle"))

    # -- console power switch --------------------------------------------
    def test_switch_defaults_to_enabled(self):
        client = self.client()
        self.assertTrue(client.enabled)
        self.assertTrue(client.status()["enabled"])

    def test_switch_off_is_quiet_and_blocks_everything(self):
        client = self.client()
        client.status()
        before = self.fake.status_requests
        state = client.set_enabled(False)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["stage"], "disabled")
        self.assertFalse(state["online"])
        # No HTTP request to the sidecar while switched off.
        self.assertEqual(self.fake.status_requests, before)
        refused = client.control("pause")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"], "Roon 集成已关闭")
        self.assertFalse(client.routed_to_roon("idle"))
        client.status()
        self.assertEqual(self.fake.status_requests, before)

    def test_switch_persists_and_configure_keeps_it(self):
        first = self.client()
        first.configure(target="roon", zone_id="zone-1")
        first.set_enabled(False)
        saved = json.loads(self.state_path.read_text())
        self.assertFalse(saved["enabled"])
        second = self.client()
        self.assertFalse(second.enabled)
        self.assertEqual(second.target, "roon")
        self.assertEqual(second.status()["stage"], "disabled")
        second.configure(target="auto")
        self.assertFalse(json.loads(self.state_path.read_text())["enabled"])
        second.set_enabled(True)
        third = self.client()
        self.assertTrue(third.enabled)
        self.assertEqual(third.status()["stage"], "ready")


class RoonApiTest(unittest.TestCase):
    """The real make_handler routes and the device command queue."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.fake = FakeSidecar()
        # Never let the poll loop reach the real Netease/local library.
        self.lyrics_patch = mock.patch.object(
            server_module.music_module, "lyrics_for_track",
            return_value=[{"t": 0, "text": "测试歌词"}])
        self.lyrics_mock = self.lyrics_patch.start()
        self.addCleanup(self.lyrics_patch.stop)
        self.config = config_module.create_config(root / "config.json")
        self.config["_root"] = self.tmp.name
        self.config["audio_runtime"] = str(root / "runtime")
        self.config["roon_bridge_url"] = self.fake.url
        self.engine = Engine(self.config,
                             state_file=str(root / "state.json"))
        self.token = self.config["token"]
        handler = server_module.make_handler(self.engine, self.token)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.02},
                                       daemon=True)
        self.thread.start()
        # The poll loop wakes up every 1.2 s; fill the cache synchronously
        # so voice-routing assertions do not depend on that timing.
        self.engine.roon_client.status()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.engine.close()
        self.fake.close()
        self.tmp.cleanup()

    def get(self, path):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            headers={"Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(request, timeout=10) as reply:
            return json.loads(reply.read())

    def post(self, path, body):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(request, timeout=10) as reply:
            return json.loads(reply.read())

    def say(self, text, seq):
        return self.post("/api/event", {
            "type": "asr_final", "text": text,
            "request_id": "voice-%d" % seq, "source": "rokid"})

    def test_status_and_control_endpoints(self):
        status = self.get("/api/roon/status")
        self.assertTrue(status["online"])
        result = self.post("/api/roon/control", {"action": "pause"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.fake.controls[-1]["action"], "pause")

    def test_settings_endpoint_changes_the_voice_target(self):
        result = self.post("/api/roon/settings", {
            "target": "roon", "zone_id": "zone-1"})
        self.assertEqual(result["target"], "roon")
        self.assertTrue(any(call.get("zone_id") == "zone-1"
                            for call in self.fake.settings))

    def test_auto_voice_control_goes_to_roon_when_local_is_idle(self):
        result = self.say("暂停", 1)
        self.assertEqual(result["intent"], "roon_control")
        self.assertTrue(result["applied"])
        self.assertEqual(self.fake.controls[-1]["action"], "pause")

    def test_local_player_wins_in_auto_mode(self):
        self.engine.audio_state = "playing"
        result = self.say("下一首", 2)
        # The local path needs a music library; what matters is that Roon
        # was NOT asked to change tracks.
        self.assertNotEqual(result["intent"], "roon_control")
        self.assertFalse(any(c["action"] == "next"
                             for c in self.fake.controls))

    def test_local_target_never_touches_roon(self):
        self.post("/api/roon/settings", {"target": "local"})
        self.say("暂停", 3)
        self.assertFalse(self.fake.controls)

    def test_volume_words_go_to_roon(self):
        result = self.say("调大音量", 4)
        self.assertEqual(result["intent"], "roon_volume")
        self.assertEqual(self.fake.controls[-1]["action"], "volume_up")

    def test_polling_emits_a_display_only_command(self):
        commands = []
        for _ in range(30):
            commands = [c for c in self.engine.pending(0)
                        if c["type"] == "roon_now"]
            if commands:
                break
            time.sleep(0.1)
        self.assertTrue(commands, "the poll loop never pushed roon_now")
        command = commands[-1]
        self.assertTrue(command["active"])
        self.assertEqual(command["title"], "晴天")
        self.assertEqual(command["artist"], "周杰伦")
        self.assertEqual(command["zone"], "客厅 AirPlay")
        self.assertTrue(command["cover"].startswith("/api/art/"))
        self.assertEqual(command["lyrics"][0]["text"], "测试歌词")

    def test_lyrics_are_looked_up_once_per_track(self):
        for _ in range(30):
            if any(c["type"] == "roon_now"
                   for c in self.engine.pending(0)):
                break
            time.sleep(0.1)
        calls = self.lyrics_mock.call_count
        self.assertGreaterEqual(calls, 1)
        time.sleep(1.5)
        self.assertEqual(self.lyrics_mock.call_count, calls)


class RoonPowerApiTest(unittest.TestCase):
    """The console switch: one call turns off state, process and voice."""

    class Power:
        def __init__(self):
            self.calls = []

        def apply(self, enabled):
            self.calls.append(bool(enabled))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.fake = FakeSidecar()
        self.power = self.Power()
        self.lyrics_patch = mock.patch.object(
            server_module.music_module, "lyrics_for_track",
            return_value=[{"t": 0, "text": "测试歌词"}])
        self.lyrics_mock = self.lyrics_patch.start()
        self.addCleanup(self.lyrics_patch.stop)
        self.config = config_module.create_config(root / "config.json")
        self.config["_root"] = self.tmp.name
        self.config["audio_runtime"] = str(root / "runtime")
        self.config["roon_bridge_url"] = self.fake.url
        self.engine = Engine(self.config,
                             state_file=str(root / "state.json"))
        self.token = self.config["token"]
        handler = server_module.make_handler(self.engine, self.token,
                                             roon_power=self.power)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.02},
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.engine.close()
        self.fake.close()
        self.tmp.cleanup()

    def post(self, path, body):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(request, timeout=10) as reply:
            return json.loads(reply.read())

    def test_switch_off_stops_the_sidecar_and_clears_the_screen(self):
        result = self.post("/api/roon/power", {"enabled": False})
        self.assertTrue(result["ok"])
        self.assertFalse(result["enabled"])
        self.assertEqual(result["stage"], "disabled")
        self.assertEqual(result["power_applied"], True)
        self.assertEqual(self.power.calls, [False])
        clears = [c for c in self.engine.pending(0)
                  if c["type"] == "roon_now" and not c.get("active")]
        self.assertTrue(clears, "the speaker's Roon screen was not cleared")
        saved = json.loads((Path(self.config["audio_runtime"])
                            / "roon-target.json").read_text())
        self.assertFalse(saved["enabled"])

    def test_voice_returns_to_local_when_roon_is_off(self):
        self.post("/api/roon/power", {"enabled": False})
        result = self.post("/api/event", {
            "type": "asr_final", "text": "暂停",
            "request_id": "voice-power", "source": "rokid"})
        self.assertEqual(result["intent"], "music_control")
        self.assertFalse(self.fake.controls)

    def test_switch_back_on_restarts_the_sidecar(self):
        self.post("/api/roon/power", {"enabled": False})
        result = self.post("/api/roon/power", {"enabled": True})
        self.assertTrue(result["enabled"])
        # The sidecar needs a moment to listen; the API says "starting"
        # rather than the misleading "offline".
        self.assertEqual(result["stage"], "starting")
        self.assertEqual(result["error"], "")
        self.assertEqual(self.power.calls, [False, True])
        self.assertEqual(self.engine.roon_client.status()["stage"], "ready")

    def test_string_false_from_a_form_is_accepted(self):
        result = self.post("/api/roon/power", {"enabled": "false"})
        self.assertFalse(result["enabled"])


class RoonManageTest(unittest.TestCase):
    def test_the_switch_decides_whether_start_launches_the_sidecar(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        try:
            import manage
        except ImportError:
            self.skipTest("manage.py unavailable")
        original = manage.RUNTIME
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manage.RUNTIME = root
            try:
                self.assertTrue(manage.roon_enabled())
                state = root / "roon-target.json"
                state.write_text(json.dumps({"enabled": False,
                                             "target": "auto"}))
                self.assertFalse(manage.roon_enabled())
                state.write_text(json.dumps({"enabled": True}))
                self.assertTrue(manage.roon_enabled())
                state.write_text("not json")
                self.assertTrue(manage.roon_enabled())
            finally:
                manage.RUNTIME = original


class RoonZonesJsTest(unittest.TestCase):
    """The sidecar once mapped Object.keys(zoneMap) into simplify(), which
    turned every zone into a blank shell. Keep that regression covered.
    """

    def test_zone_shape_and_events(self):
        app_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(app_root))
        try:
            import manage
        except ImportError:
            self.skipTest("manage.py unavailable")
        node = manage.node_binary()
        if not node:
            self.skipTest("node binary unavailable")
        script = Path(__file__).with_name("roon_zones_test.js")
        result = subprocess.run([node, str(script)], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)
        self.assertIn("checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
