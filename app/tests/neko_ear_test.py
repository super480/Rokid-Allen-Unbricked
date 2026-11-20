"""S7: the catgirl's ear-only mode (speaker = microphone, Mac speaks).

Host-side only. Nothing here plays audio on the device: the command queue
is inspected instead of being delivered to the speaker. The Mac speaker
is replaced by an unavailable Mock, so ear-mode tests cannot invoke say.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
import mac_speech  # noqa: E402
import server as server_module  # noqa: E402
from engine import Engine  # noqa: E402


def make_engine(root):
    config = config_module.create_config(root / "config.json")
    config["_root"] = str(root)
    config["_path"] = str(root / "config.json")
    # Never let a unit test reach the developer's real Roon sidecar.
    config["roon_bridge_url"] = "http://127.0.0.1:1"
    engine = Engine(config, state_file=str(root / "state.json"))
    # Plumbing tests must never fall through to the Mac's real `say` process.
    # Voice behaviour is covered separately with FakeRunner.
    engine.mac_speaker = Mock(spec=mac_speech.MacSpeech)
    engine.mac_speaker.available.return_value = False
    engine.mac_speaker.busy = False
    engine.mac_speaker.last_text = ""
    return config, engine


class NekoEarModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config, self.engine = make_engine(self.root)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def of_type(self, kind):
        return [c for c in self.engine.pending(0) if c["type"] == kind]

    def test_default_is_off_and_the_speaker_reads_the_reply(self):
        self.assertFalse(self.engine.neko_ear_only_settings()["enabled"])
        self.engine.deliver_speech("这是一条测试回复。", "neko", "rid-1")
        self.assertEqual(len(self.of_type("speak")), 1)

    def test_other_assistants_still_speak(self):
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("好的。", "hermes", "rid-3")
        self.assertEqual(len(self.of_type("speak")), 1)

    def test_proactive_is_gated_without_losing_the_saved_settings(self):
        self.engine.select("neko", announce=False)
        self.engine.set_neko_proactive(enabled=True)
        now = time.time()
        before = self.engine._proactive_gate(now=now)
        self.engine.set_neko_ear_only(enabled=True)
        self.assertFalse(self.engine._proactive_gate(now=now))
        # The saved proactive switch is untouched and resumes on exit.
        self.assertTrue(self.engine.neko_proactive_settings()["enabled"])
        self.engine.set_neko_ear_only(enabled=False)
        self.assertEqual(self.engine._proactive_gate(now=now), before)

    def test_the_switch_is_persisted_and_reported_in_status(self):
        self.engine.set_neko_ear_only(enabled=True)
        saved = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["neko"]["ear_only"])
        self.assertTrue(self.engine.status()["neko_ear_only"]["enabled"])
        self.engine.set_neko_ear_only(enabled=False)
        self.assertFalse(self.engine.status()["neko_ear_only"]["enabled"])


class NekoEarApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config, self.engine = make_engine(self.root)
        self.token = self.config["token"]
        handler = server_module.make_handler(self.engine, self.token)
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
        self.tmp.cleanup()

    def get(self, path):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            headers={"Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def post(self, path, body):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def test_the_console_switch_round_trips_over_http(self):
        self.assertFalse(self.get("/api/neko/ear-only")["enabled"])
        self.assertTrue(self.post("/api/neko/ear-only",
                                  {"enabled": True})["enabled"])
        self.assertTrue(self.get("/api/status")["neko_ear_only"]["enabled"])
        self.engine.deliver_speech("测试回复。", "neko", "rid-http")
        speaks = [c for c in self.engine.pending(0) if c["type"] == "speak"]
        self.assertEqual(speaks, [])
        self.post("/api/neko/ear-only", {"enabled": False})


class NekoEarConsoleTest(unittest.TestCase):
    def test_the_switch_is_hidden_but_the_endpoint_stays(self):
        # The ear-only card was removed from the settings page on request;
        # the backend switch stays addressable by API and by voice/script.
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        self.assertNotIn('id="nekoearonly"', page)
        self.assertNotIn("function loadNekoEar", page)
        self.assertNotIn("若琪只当猫娘的耳朵", page)
        server = (Path(__file__).resolve().parents[1] / "host" /
                  "server.py").read_text(encoding="utf-8")
        self.assertIn('"/api/neko/ear-only"', server)
