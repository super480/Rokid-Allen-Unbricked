"""Spoken song requests: "我想听晴天" plays the local copy, or Netease.

Covers the word parser, the relevance ranking that stops "晴天" from
playing "晴天雨", the engine short-circuit, and the local-first HTTP hook.
No test plays audio on the device: everything here is host side only.
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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
import intent  # noqa: E402
import music as music_module  # noqa: E402
import server as server_module  # noqa: E402
from engine import Engine  # noqa: E402


class MusicRequestWordsTest(unittest.TestCase):
    def test_song_requests_are_parsed(self):
        cases = {
            "我想听晴天": ("晴天", "", False),
            "我要听晴天吧": ("晴天", "", False),
            "若琪，我想听晴天": ("晴天", "", False),
            "放一首周杰伦的晴天": ("晴天", "周杰伦", False),
            "我想听周杰伦的晴天": ("晴天", "周杰伦", False),
            "来一首夜的钢琴曲": ("钢琴曲", "夜", False),
            "播放晴天": ("晴天", "", False),
        }
        for text, (title, artist, random_) in cases.items():
            parsed = intent.music_request(text)
            self.assertIsNotNone(parsed, text)
            self.assertEqual(parsed["title"], title, text)
            self.assertEqual(parsed["artist"], artist, text)
            self.assertEqual(parsed["random"], random_, text)

    def test_requests_without_a_name_mean_a_random_local_song(self):
        for text in ("来首歌", "来首歌吧", "放点音乐", "听歌", "播放音乐",
                     "随便放一首", "我想听"):
            parsed = intent.music_request(text)
            self.assertIsNotNone(parsed, text)
            self.assertTrue(parsed["random"], text)

    def test_questions_and_controls_are_not_song_requests(self):
        for text in ("我想听你讲讲为什么", "我想听什么歌", "帮我解释一下晴天",
                     "下一首", "暂停", "继续播放", "停止播放",
                     "今天天气怎么样", "听我说", "放心吧", ""):
            self.assertIsNone(intent.music_request(text), text)

    def test_song_requests_are_not_factory_skills(self):
        pattern = config_module.DEFAULTS["direct"]["factory_owns"]
        for text in ("我想听晴天", "放一首周杰伦", "来首歌", "播放音乐",
                     "听歌", "放点音乐"):
            self.assertFalse(intent.factory_owns(text, pattern), text)
        # weather turns stopped being factory skills in D047
        for text in ("现在几点了", "明天八点提醒我"):
            self.assertTrue(intent.factory_owns(text, pattern), text)


class RankingTest(unittest.TestCase):
    def test_whole_phrase_only(self):
        self.assertFalse(music_module.phrase_hit("晴天雨", "晴天"))
        self.assertFalse(music_module.phrase_hit("晴天雨.flac", "晴天"))
        self.assertTrue(music_module.phrase_hit("晴天", "晴天"))
        self.assertTrue(music_module.phrase_hit("晴天 (Live)", "晴天"))
        self.assertTrue(music_module.phrase_hit("周杰伦 - 晴天.flac", "晴天"))
        self.assertTrue(music_module.phrase_hit("晴天（Live）", "晴天"))

    def test_exact_local_name_beats_a_folder_name_hit(self):
        request = {"phrase": "晴天", "title": "晴天", "artist": ""}
        exact = {"kind": "local", "id": "a", "name": "晴天",
                 "artist": "周杰伦", "file": "周杰伦/晴天.flac"}
        folder = {"kind": "local", "id": "b", "name": "说好不哭",
                  "artist": "周杰伦", "file": "周杰伦/说好不哭.flac"}
        near = {"kind": "local", "id": "c", "name": "晴天雨",
                "artist": "本地音乐", "file": "晴天雨.flac"}
        self.assertGreater(music_module.rank_local_track(exact, request),
                           music_module.rank_local_track(folder, request))
        self.assertEqual(music_module.rank_local_track(near, request), 0)

    def test_artist_adds_weight_online(self):
        request = {"phrase": "周杰伦的晴天", "title": "晴天",
                   "artist": "周杰伦"}
        wrong = {"name": "晴天", "artist": "TOYOKI"}
        right = {"name": "晴天", "artist": "周杰伦"}
        self.assertGreater(music_module.rank_online_track(right, request),
                           music_module.rank_online_track(wrong, request))


class MusicRequestEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = config_module.create_config(root / "config.json")
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.engine = Engine(self.config, state_file=str(root / "state.json"))
        self.calls = []
        self.engine.music_request_hook = self._hook

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def _hook(self, request):
        self.calls.append(request)
        return {"ok": True, "title": "晴天", "source": "local"}

    def _say(self, text, offset=0):
        return self.engine.handle_event({
            "type": "asr_final", "text": text,
            "request_id": "voice-%s-%d" % (text, offset),
            "source": "rokid",
        })

    def test_a_request_never_reaches_an_assistant(self):
        self.engine.select("hermes", announce=False)
        result = self._say("我想听晴天")
        self.assertTrue(result["applied"], result)
        self.assertEqual(result["title"], "晴天")
        self.assertEqual(self.engine.jobs.qsize(), 0)
        self.assertFalse([c for c in self.engine.pending(0)
                          if c["type"] == "speak"])
        self.assertFalse([e for e in self.engine.transcript
                          if e["role"] == "user"])
        self.assertEqual(self.calls[0]["title"], "晴天")

    def test_failure_says_one_short_line(self):
        self.engine.music_request_hook = lambda request: {
            "ok": False, "error": "没有找到这首歌"}
        result = self._say("我想听不存在的歌")
        self.assertFalse(result["applied"])
        speaks = [c for c in self.engine.pending(0) if c["type"] == "speak"]
        self.assertEqual(len(speaks), 1)
        self.assertEqual(speaks[0]["text"], "没找到这首歌。")
        self.assertLessEqual(len(speaks[0]["text"]), 10)
        self.assertEqual(speaks[0]["agent"], "none")

    def test_hint_matches_the_failure(self):
        self.engine.music_request_hook = lambda request: {
            "ok": False, "error": "连不上 N.E.K.O."}
        result = self._say("我想听晴天")
        self.assertEqual(result["hint"], "网上暂时放不了。")

    def test_hook_exception_is_reported_not_raised(self):
        def broken(request):
            raise ValueError("索引炸了")
        self.engine.music_request_hook = broken
        result = self._say("我想听晴天")
        self.assertFalse(result["applied"])
        self.assertIn("索引炸了", result["reason"])


class MusicRequestApiTest(unittest.TestCase):
    """The real make_handler hook: local index first, Netease fallback."""

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

    def seed(self, names):
        folder = Path(self.tmp.name) / "songs"
        folder.mkdir(exist_ok=True)
        for name in names:
            (folder / name).write_bytes(b"ID3" + b"\x00" * 64)
        self.post("/api/music/folders", {"folders": [str(folder)]})
        for _ in range(300):
            if self.get("/api/music/folders")["index"]["state"] == "ready":
                return
            time.sleep(0.01)
        self.fail("the local index never became ready")

    def say(self, text):
        return self.post("/api/event", {
            "type": "asr_final", "text": text,
            "request_id": "voice-" + text, "source": "rokid"})

    def audio_commands(self):
        return [c for c in self.engine.pending(0) if c["type"] == "audio"]

    def test_parse_endpoint_is_read_only(self):
        result = self.post("/api/music/parse", {"text": "我想听周杰伦的晴天"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["parsed"]["artist"], "周杰伦")
        self.assertEqual(result["parsed"]["title"], "晴天")
        self.assertEqual(len(self.engine.pending(0)), 0)

    def test_local_song_wins(self):
        self.seed(["晴天.flac", "林俊杰 - 不为谁而作的歌.flac"])
        result = self.say("我想听晴天")
        self.assertTrue(result["applied"], result)
        self.assertEqual(result["source"], "local")
        self.assertEqual(self.audio_commands()[-1]["title"], "晴天")

    def test_a_near_miss_is_not_the_song_and_falls_back_online(self):
        self.seed(["晴天雨.flac"])
        online = [{"kind": "online", "id": "online:1", "name": "晴天",
                   "artist": "周杰伦", "url": "/api/music/play/netease/1"}]
        with mock.patch.object(music_module.MusicProvider, "search",
                               return_value=online), \
             mock.patch.object(music_module.MusicProvider, "resolve",
                               return_value=(b"ID3" + b"\x00" * 64,
                                             ".mp3", "晴天")), \
             mock.patch.object(music_module.MusicProvider, "validate"), \
             mock.patch.object(music_module.MusicProvider, "artwork",
                               return_value=None), \
             mock.patch.object(music_module.MusicProvider, "lyrics",
                               return_value=[]):
            result = self.say("我想听晴天")
        self.assertTrue(result["applied"], result)
        self.assertEqual(result["source"], "online")
        self.assertEqual(self.audio_commands()[-1]["title"], "晴天")

    def test_random_request_plays_a_local_song(self):
        self.seed(["第一首.flac", "第二首.flac"])
        result = self.say("来首歌")
        self.assertTrue(result["applied"], result)
        self.assertEqual(result["source"], "local")
        self.assertEqual(len(self.audio_commands()), 1)

    def test_nothing_found_speaks_a_short_hint(self):
        self.seed(["别的手.flac"])
        with mock.patch.object(music_module.MusicProvider, "search",
                               return_value=[]):
            result = self.say("我想听根本不存在的歌")
        self.assertFalse(result["applied"], result)
        speaks = [c for c in self.engine.pending(0) if c["type"] == "speak"]
        self.assertEqual(len(speaks), 1)
        self.assertLessEqual(len(speaks[0]["text"]), 10)


if __name__ == "__main__":
    unittest.main()
