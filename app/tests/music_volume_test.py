"""S6: voice volume control and the factory's canned music notice.

Host-side only. Nothing here plays audio on the device: the commands are
inspected in the queue, and the device's acknowledgement is simulated.
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
import intent  # noqa: E402
import server as server_module  # noqa: E402
from engine import Engine  # noqa: E402


class VolumeWordsTest(unittest.TestCase):
    def test_relative_commands(self):
        cases = {
            "调大音量": "up", "把音量调大一点": "up", "大声点": "up",
            "调小": "down", "小声点": "down", "降低音量": "down",
            "静音": "mute", "别出声": "mute",
            "取消静音": "unmute", "恢复声音": "unmute",
        }
        for text, action in cases.items():
            parsed = intent.music_volume(text)
            self.assertIsNotNone(parsed, text)
            self.assertEqual(parsed["action"], action, text)
            self.assertIsNone(parsed["value"], text)

    def test_absolute_commands(self):
        cases = {
            "音量调到50": 50, "调到30": 30, "音量百分之八十": 80,
            "音量三十": 30, "调到一百": 100, "设为0": 0,
        }
        for text, value in cases.items():
            parsed = intent.music_volume(text)
            self.assertIsNotNone(parsed, text)
            self.assertEqual(parsed["action"], "set", text)
            self.assertEqual(parsed["value"], value, text)

    def test_questions_and_other_turns_are_not_volume_commands(self):
        for text in ("大声朗读这首诗", "音量是什么意思", "把音量调大好不好",
                     "今天天气怎么样", "我想听晴天", "下一首",
                     "暂停", "停止播放", ""):
            self.assertIsNone(intent.music_volume(text), text)

    def test_volume_words_are_not_factory_skills(self):
        pattern = config_module.DEFAULTS["direct"]["factory_owns"]
        for text in ("调大音量", "把音量调大一点", "静音", "取消静音",
                     "音量调到50"):
            self.assertFalse(intent.factory_owns(text, pattern), text)
        # weather turns stopped being factory skills in D047
        for text in ("现在几点了", "明天八点提醒我"):
            self.assertTrue(intent.factory_owns(text, pattern), text)


class FactoryNoticeTest(unittest.TestCase):
    def test_the_canned_notice_is_recognised(self):
        for text in ("若琪官方与酷哥合作到期，请用蓝牙播放",
                     "合作到期请用蓝牙播放", "请用蓝牙播放",
                     "版权合作到期"):
            self.assertTrue(intent.factory_notice(text), text)

    def test_the_content_service_expiry_notice_is_recognised(self):
        # The exact clip the speaker repeats when the free content service
        # lapses, plus the variants the ASR tends to produce.
        for text in ("由于你免费的内容服务已经到期了，请在若琪APP我的内容服务中续费",
                     "您的免费内容服务已到期，请续费",
                     "请在若琪app我的内容服务中续费",
                     "内容服务已经到期"):
            self.assertTrue(intent.factory_notice(text), text)

    def test_ordinary_sentences_are_kept(self):
        for text in ("用蓝牙播放音乐", "我想听晴天", "打开蓝牙",
                     "今天天气怎么样", "内容服务怎么收费",
                     "帮我续费", "服务到期了吗", ""):
            self.assertFalse(intent.factory_notice(text), text)


class VolumeEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.config = config_module.create_config(root / "config.json")
        self.config["_root"] = self.tmp.name
        self.config["_path"] = str(root / "config.json")
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.engine = Engine(self.config, state_file=str(root / "state.json"))

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def _say(self, text, offset=0):
        return self.engine.handle_event({
            "type": "asr_final", "text": text,
            "request_id": "voice-%s-%d" % (text, offset),
            "source": "rokid"})

    def _last(self, kind):
        matches = [c for c in self.engine.pending(0) if c["type"] == kind]
        self.assertTrue(matches, kind)
        return matches[-1]

    def test_a_volume_turn_never_reaches_an_assistant(self):
        self.engine.select("hermes", announce=False)
        result = self._say("调大音量")
        self.assertEqual(result["intent"], "music_volume")
        self.assertTrue(result["applied"])
        command = self._last("set_volume")
        self.assertEqual(command["action"], "up")
        self.assertEqual(self.engine.jobs.qsize(), 0)
        self.assertFalse([e for e in self.engine.transcript
                          if e["role"] == "user"])

    def test_absolute_value_travels_with_the_command(self):
        self._say("音量调到50")
        command = self._last("set_volume")
        self.assertEqual(command["action"], "set")
        self.assertEqual(command["value"], 50)

    def test_mute_and_unmute_flip_the_state(self):
        self._say("静音")
        self.assertTrue(self.engine.status()["audio"]["muted"])
        self._say("取消静音")
        self.assertFalse(self.engine.status()["audio"]["muted"])

    def test_device_acknowledgement_updates_status(self):
        result = self.engine.handle_event({
            "type": "volume_state", "source": "audio",
            "volume": 9, "max": 15, "muted": False})
        self.assertEqual(result["intent"], "volume_ack")
        audio = self.engine.status()["audio"]
        self.assertEqual(audio["volume"], 9)
        self.assertEqual(audio["volume_max"], 15)
        self.assertFalse(audio["muted"])
        self.assertEqual(self.engine.jobs.qsize(), 0)

    def test_factory_notice_is_dropped_like_an_echo(self):
        result = self._say("若琪官方与酷哥合作到期，请用蓝牙播放")
        self.assertEqual(result["intent"], "factory_notice")
        self.assertEqual(self.engine.jobs.qsize(), 0)
        self.assertEqual(len(self.engine.pending(0)), 0)

    def test_content_service_notice_does_not_produce_a_reply(self):
        # Answering this clip would make the speaker say it again, forever.
        result = self._say("由于你免费的内容服务已经到期了，请在若琪APP我的内容服务中续费")
        self.assertEqual(result["intent"], "factory_notice")
        self.assertEqual(self.engine.jobs.qsize(), 0)
        self.assertEqual(len(self.engine.pending(0)), 0)

    def test_the_expiry_notice_switch_is_on_by_default(self):
        self.assertTrue(self.engine.factory_notice_settings()["enabled"])
        self.assertTrue(self.engine.status()["factory_notice"]["enabled"])

    def test_turning_the_switch_off_lets_the_notice_through_and_is_saved(self):
        result = self.engine.set_factory_notice(enabled=False)
        self.assertFalse(result["enabled"])
        # The notice is no longer muted: it takes the ordinary turn path.
        said = self._say("由于你免费的内容服务已经到期了，请在若琪APP我的内容服务中续费")
        self.assertNotEqual(said["intent"], "factory_notice")
        self.assertFalse(self.engine.status()["factory_notice"]["enabled"])
        saved = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertFalse(saved["suppress_factory_notice"])

    def test_unknown_action_is_rejected(self):
        result = self.engine.apply_music_volume({"action": "explode"})
        self.assertFalse(result["applied"])


class VolumeApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = config_module.create_config(root / "config.json")
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.config["audio_runtime"] = str(root / "runtime")
        self.engine = Engine(self.config, state_file=str(root / "state.json"))
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

    def test_the_other_page_switch_round_trips_over_http(self):
        self.assertTrue(self.get("/api/factory-notice")["enabled"])
        result = self.post("/api/factory-notice", {"enabled": False})
        self.assertFalse(result["enabled"])
        self.assertFalse(self.get("/api/factory-notice")["enabled"])
        self.assertFalse(self.get("/api/status")["factory_notice"]["enabled"])
        notice = self.post("/api/event", {
            "type": "asr_final",
            "text": "由于你免费的内容服务已经到期了，请在若琪APP我的内容服务中续费",
            "request_id": "notice-switch-off", "source": "rokid"})
        self.assertNotEqual(notice["intent"], "factory_notice")
        self.post("/api/factory-notice", {"enabled": True})

    def test_console_button_and_voice_share_the_command(self):
        result = self.post("/api/music/volume", {"action": "set", "value": 40})
        self.assertTrue(result["applied"], result)
        commands = [c for c in self.engine.pending(0)
                    if c["type"] == "set_volume"]
        self.assertEqual(commands[-1]["value"], 40)

        result = self.post("/api/event", {
            "type": "asr_final", "text": "调小音量",
            "request_id": "voice-volume", "source": "rokid"})
        self.assertEqual(result["intent"], "music_volume")
        commands = [c for c in self.engine.pending(0)
                    if c["type"] == "set_volume"]
        self.assertEqual(commands[-1]["action"], "down")
        self.assertEqual(len(commands), 2)

    def test_ack_reaches_status(self):
        result = self.post("/api/event", {
            "type": "volume_state", "source": "audio",
            "volume": 7, "max": 15, "muted": True})
        self.assertEqual(result["intent"], "volume_ack")
        audio = self.get("/api/status")["audio"]
        self.assertEqual(audio["volume"], 7)
        self.assertTrue(audio["muted"])


if __name__ == "__main__":
    unittest.main()
