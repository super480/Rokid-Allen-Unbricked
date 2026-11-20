"""S8: the console's batch alias editor for every assistant's wake word.

The cloud re-checks a wake: its ASR text must contain one of the aliases,
or it judges the wake fake and cuts the microphone after 0.7s. This card
edits all of them at once. Saving goes through set_wake(), which updates
the in-memory config and emits a wake command, so no restart is needed.
"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
import server as server_module  # noqa: E402
from engine import Engine  # noqa: E402


def make_engine(root):
    config = config_module.create_config(root / "config.json")
    config["_root"] = str(root)
    config["_path"] = str(root / "config.json")
    config["roon_bridge_url"] = "http://127.0.0.1:1"
    return config, Engine(config, state_file=str(root / "state.json"))


class WakeAliasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config, self.engine = make_engine(self.root)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def test_one_save_updates_every_assistant(self):
        result = self.engine.set_wake(aliases_map={
            "workbuddy": "八八|八弟|body",
            "hermes": "小鹤",
        })
        self.assertEqual(result["agents"]["workbuddy"]["aliases"],
                         ["八八", "八弟", "body"])
        self.assertEqual(result["agents"]["hermes"]["aliases"], ["小鹤"])
        vt = result["agents"]["workbuddy"]["vt_word"]
        self.assertTrue(vt.startswith("巴蒂|"))
        self.assertIn("八八", vt)

    def test_an_unknown_agent_aborts_the_whole_batch(self):
        before = self.engine.wake_settings()
        with self.assertRaises(ValueError):
            self.engine.set_wake(aliases_map={
                "workbuddy": "新写法", "not-an-agent": "坏"})
        self.assertEqual(self.engine.wake_settings(), before)

    def test_name_duplicates_and_pipe_are_dropped(self):
        result = self.engine.set_wake(aliases_map={
            "workbuddy": ["巴蒂", "八八", "八八", "a|b", "  "]})
        self.assertEqual(result["agents"]["workbuddy"]["aliases"], ["八八"])

    def test_an_empty_value_clears_the_aliases(self):
        result = self.engine.set_wake(aliases_map={"workbuddy": ""})
        self.assertEqual(result["agents"]["workbuddy"]["aliases"], [])
        self.assertEqual(result["agents"]["workbuddy"]["vt_word"], "巴蒂")

    def test_the_batch_is_persisted(self):
        self.engine.set_wake(aliases_map={"workbuddy": "八八"})
        saved = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["wake"]["agents"]["workbuddy"]["aliases"],
                         ["八八"])

    def test_saving_pushes_a_wake_command_so_no_restart_is_needed(self):
        before = len([c for c in self.engine.pending(0)
                      if c["type"] == "wake"])
        self.engine.set_wake(aliases_map={"workbuddy": "八八"})
        pushed = [c for c in self.engine.pending(0) if c["type"] == "wake"]
        self.assertEqual(len(pushed), before + 1)
        self.assertIn("八八", pushed[-1]["agents"]["workbuddy"]["vt_word"])


class WakeAliasesApiTest(unittest.TestCase):
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

    def test_the_console_can_save_every_alias_in_one_request(self):
        result = self.post("/api/wake", {"aliases_map": {
            "workbuddy": "八八|八弟", "hermes": "小鹤"}})
        self.assertIn("八八", result["agents"]["workbuddy"]["vt_word"])
        self.assertEqual(result["agents"]["hermes"]["aliases"], ["小鹤"])
        fresh = self.get("/api/wake")
        self.assertEqual(fresh["agents"]["workbuddy"]["aliases"],
                         ["八八", "八弟"])


class WakeAliasesConsoleTest(unittest.TestCase):
    def test_the_card_and_its_single_save_button_exist(self):
        page = (Path(__file__).resolve().parents[1] / "host" / "web" /
                "console.html").read_text(encoding="utf-8")
        for marker in ('id="wakealiascard"', 'id="wakealiasrows"',
                       'id="wakealiasesave"', "保存别名",
                       "aliases_map", "function renderWakeAliases",
                       "不需要重启", "activ fake"):
            self.assertIn(marker, page)
