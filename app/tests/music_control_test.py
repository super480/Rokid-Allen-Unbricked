"""Voice playback control for the speaker's own MediaPlayer.

The factory used to swallow "暂停 / 下一首 / 停止播放", and the host had no
command for them either. These tests cover the whole new path: the word
list, the factory rule, the engine's short-circuit, the device commands,
and the console's HTTP entry point.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
import intent  # noqa: E402
import server as server_module  # noqa: E402
from engine import Engine  # noqa: E402


CONTROLS = {
    "pause": ("暂停", "暂停一下", "若琪，暂停播放", "先暂停"),
    "resume": ("继续播放", "接着放", "恢复播放", "继续听"),
    "next": ("下一首", "换一首", "切歌", "若琪，下一首"),
    "prev": ("上一首", "回到上一首", "切上一首"),
    "stop": ("停止播放", "别放了", "关掉音乐", "停止音乐"),
}


class MusicControlWordsTest(unittest.TestCase):
    def test_every_control_phrase_is_recognised(self):
        for action, phrases in CONTROLS.items():
            for phrase in phrases:
                self.assertEqual(intent.music_control(phrase), action, phrase)

    def test_ordinary_questions_are_not_controls(self):
        for text in ("帮我讲讲暂停是什么意思", "今天天气怎么样",
                     "下一首歌是谁写的来着帮我查一下",
                     "播放周杰伦的歌", "", "放一首周杰伦"):
            self.assertEqual(intent.music_control(text), "", text)

    def test_controls_are_not_factory_skills(self):
        pattern = config_module.DEFAULTS["direct"]["factory_owns"]
        for phrases in CONTROLS.values():
            for phrase in phrases:
                self.assertFalse(intent.factory_owns(phrase, pattern), phrase)

    def test_song_requests_now_reach_the_host(self):
        # S4 took song requests over completely: the bridge plays from its
        # own library, the factory's cloud catalogue is no longer used.
        pattern = config_module.DEFAULTS["direct"]["factory_owns"]
        for text in ("放一首周杰伦", "来首歌", "听歌", "播放音乐"):
            self.assertFalse(intent.factory_owns(text, pattern), text)


class MusicControlEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = config_module.create_config(root / "config.json")
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.engine = Engine(self.config, state_file=str(root / "state.json"))
        self.hook_calls = []
        self.engine.music_hook = self._hook

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def _hook(self, action):
        self.hook_calls.append(action)
        return {"ok": True, "title": "换的歌"}

    def _say(self, text, offset=0):
        return self.engine.handle_event({
            "type": "asr_final", "text": text,
            "request_id": "voice-%s-%d" % (text, offset),
            "source": "rokid",
        })

    def _start(self):
        self.engine.play_audio("/api/audio/abc.m4a", "当前歌曲")
        self.assertEqual(self.engine.audio_state, "playing")

    def _last(self, kind):
        matches = [c for c in self.engine.pending(0) if c["type"] == kind]
        self.assertTrue(matches, kind)
        return matches[-1]

    def test_pause_emits_one_command_and_no_assistant_turn(self):
        self._start()
        result = self._say("暂停")
        self.assertEqual(result["action"], "pause")
        self.assertTrue(result["applied"])
        self._last("pause_audio")
        self.assertEqual(self.engine.audio_state, "paused")
        self.assertEqual(self.engine.jobs.qsize(), 0)
        self.assertFalse([e for e in self.engine.transcript
                          if e["role"] == "user"])

    def test_resume_only_works_from_paused(self):
        self._start()
        self.assertFalse(self.engine.apply_music_control("resume")["applied"])
        self._say("暂停")
        result = self._say("继续播放")
        self.assertTrue(result["applied"])
        self._last("resume_audio")
        self.assertEqual(self.engine.audio_state, "playing")

    def test_next_and_prev_go_through_the_library_hook(self):
        self._start()
        for word, action in (("下一首", "next"), ("上一首", "prev")):
            result = self._say(word)
            self.assertTrue(result["applied"], word)
            self.assertEqual(result["title"], "换的歌")
            self.assertEqual(self.hook_calls[-1], action)
        self.assertEqual(self.hook_calls, ["next", "prev"])

    def test_stop_clears_the_audio_state(self):
        self._start()
        result = self._say("停止播放")
        self.assertTrue(result["applied"])
        self._last("stop_audio")
        self.assertEqual(self.engine.audio_state, "idle")

    def test_a_control_with_nothing_playing_is_ignored(self):
        result = self._say("暂停")
        self.assertFalse(result["applied"])
        self.assertTrue(result["idle"])
        self.assertEqual(self.engine.jobs.qsize(), 0)
        self.assertEqual(len(self.engine.pending(0)), 0)

    def test_a_control_does_not_reach_a_selected_assistant(self):
        self.engine.select("hermes", announce=False)
        self._start()
        self._say("下一首")
        self.assertEqual(self.engine.jobs.qsize(), 0)
        self.assertEqual(self.hook_calls, ["next"])

    def test_unknown_action_is_rejected(self):
        result = self.engine.apply_music_control("volume_up")
        self.assertFalse(result["applied"])

    def test_hook_failure_is_reported_not_raised(self):
        self._start()
        self.engine.music_hook = lambda action: {"ok": False, "error": "没有上一首"}
        result = self._say("上一首")
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "没有上一首")

    def test_console_controls_work_without_a_voice_turn(self):
        self._start()
        self.assertTrue(self.engine.apply_music_control("pause")["applied"])
        self.assertTrue(self.engine.apply_music_control("resume")["applied"])
        self.assertTrue(self.engine.apply_music_control("stop")["applied"])


class MusicControlApiTest(unittest.TestCase):
    """The console buttons and spoken words share /api/music/control."""

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

    def seed_folders(self):
        """A tiny real library, since "next" draws from the local index."""
        folder = Path(self.tmp.name) / "songs"
        folder.mkdir()
        for index in range(3):
            (folder / ("第%d首.mp3" % index)).write_bytes(b"ID3" + b"\x00" * 64)
        self.post("/api/music/folders", {"folders": [str(folder)]})
        for _ in range(300):
            if self.get("/api/music/folders")["index"]["state"] == "ready":
                return
            time.sleep(0.01)
        self.fail("the local index never became ready")

    def last_command(self, kind):
        matches = [c for c in self.engine.pending(0) if c["type"] == kind]
        self.assertTrue(matches, kind)
        return matches[-1]

    def test_pause_resume_stop_round_trip(self):
        self.assertTrue(self.post("/api/music/play", {"kind": "test"})["ok"])
        result = self.post("/api/music/control", {"action": "pause"})
        self.assertTrue(result["applied"], result)
        self.assertEqual(result["state"], "paused")
        self.last_command("pause_audio")

        result = self.post("/api/music/control", {"action": "resume"})
        self.assertTrue(result["applied"], result)
        self.assertEqual(result["state"], "playing")
        self.last_command("resume_audio")

        result = self.post("/api/music/control", {"action": "stop"})
        self.assertTrue(result["applied"], result)
        self.assertEqual(result["state"], "idle")
        self.last_command("stop_audio")

    def test_next_then_prev_replays_the_previous_track(self):
        self.seed_folders()
        self.post("/api/music/play", {"kind": "test"})
        result = self.post("/api/music/control", {"action": "next"})
        self.assertTrue(result["applied"], result)
        self.assertTrue(result["title"])
        self.last_command("audio")

        result = self.post("/api/music/control", {"action": "prev"})
        self.assertTrue(result["applied"], result)
        self.assertEqual(result["title"], "试音提示")

    def test_prev_without_history_is_reported(self):
        result = self.post("/api/music/control", {"action": "prev"})
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "没有上一首")

    def test_spoken_next_uses_the_same_hook_as_the_button(self):
        self.seed_folders()
        self.post("/api/music/play", {"kind": "test"})
        result = self.engine.handle_event({
            "type": "asr_final", "text": "若琪，下一首",
            "request_id": "spoken-next", "source": "rokid"})
        self.assertTrue(result["applied"], result)
        self.assertEqual(result["action"], "next")
        self.assertTrue(result["title"])


if __name__ == "__main__":
    unittest.main()
