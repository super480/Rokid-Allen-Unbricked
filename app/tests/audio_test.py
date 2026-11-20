"""The speaker's own audio channel (S3).

The host resolves a clip, caches it, and hands the device a relative path.
The device downloads it with its normal token and plays the local file with
MediaPlayer. These tests cover the host half end to end over real HTTP;
the device half is verified on the speaker itself.
"""
import base64
import json
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import wave
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import audio as audio_module  # noqa: E402
import config as config_module  # noqa: E402
import music as music_module  # noqa: E402
import netease as netease_module  # noqa: E402
import server as server_module  # noqa: E402
from engine import Engine  # noqa: E402


class AudioCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = audio_module.AudioCache(Path(self.tmp.name) / "audio")

    def tearDown(self):
        self.tmp.cleanup()

    def test_minted_clip_can_be_read_back(self):
        clip = self.cache.put(b"RIFF" + b"\x00" * 60, ".wav", "试音")
        self.assertEqual(clip["title"], "试音")
        self.assertEqual(clip["bytes"], 64)
        target = self.cache.path_for(clip["name"])
        self.assertIsNotNone(target)
        self.assertEqual(target.read_bytes()[:4], b"RIFF")
        self.assertEqual(self.cache.content_type(clip["name"]), "audio/wav")

    def test_path_traversal_and_arbitrary_names_are_rejected(self):
        for name in ("../config.json", "..%2Fconfig.json", "abc.wav",
                     "deadbeef.png", "/etc/passwd", ""):
            self.assertIsNone(self.cache.path_for(name), name)

    def test_bad_input_is_rejected(self):
        for data, suffix in ((b"", ".wav"), (b"x" * 16, ".exe"),
                             (b"x" * (audio_module.MAX_BYTES + 1), ".mp3")):
            with self.assertRaises(ValueError):
                self.cache.put(data, suffix)

    def test_old_clips_are_swept(self):
        clip = self.cache.put(b"RIFF" + b"\x00" * 60, ".wav")
        path = self.cache.path_for(clip["name"])
        old = time.time() - audio_module.MAX_AGE - 10
        import os
        os.utime(path, (old, old))
        self.cache._sweep()
        self.assertIsNone(self.cache.path_for(clip["name"]))


class MusicProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = music_module.MusicProvider(
            {"neko": {"url": "ws://127.0.0.1:48911"}},
            local_root=Path(self.tmp.name) / "music")

    def tearDown(self):
        self.tmp.cleanup()

    def test_library_always_has_the_builtin_tracks(self):
        items = self.provider.library()
        self.assertGreaterEqual(len(items), 4)
        self.assertTrue(all(x["kind"] == "builtin" for x in items))
        self.assertTrue(all(x["file"].endswith(".mp3") for x in items))

    def test_local_files_show_up_after_indexing(self):
        root = self.provider.local_root
        root.mkdir(parents=True, exist_ok=True)
        (root / "我的一首歌.mp3").write_bytes(b"ID3" + b"\x00" * 100)
        self.provider.set_folders([str(root)])
        for _ in range(200):
            if self.provider.index.status()["state"] == "ready":
                break
            time.sleep(0.01)
        names = [x["name"] for x in self.provider.library()]
        self.assertIn("我的一首歌", names)

    def test_chime_and_silence_are_valid_wav(self):
        for maker in (music_module._chime, music_module._silence):
            data = maker()
            self.assertTrue(music_module._looks_like_audio(data, ".wav"))
            self.assertEqual(data[:4], b"RIFF")

    def test_resolve_test_and_silent(self):
        data, suffix, title = self.provider.resolve({"kind": "test"})
        self.assertEqual(suffix, ".wav")
        self.assertEqual(title, "试音提示")
        data, suffix, _ = self.provider.resolve({"kind": "silent"})
        self.provider.validate(data, suffix)

    def test_a_non_audio_response_is_rejected(self):
        with self.assertRaises(music_module.MusicError):
            self.provider.validate(b'{"detail":"Not Found"}', ".mp3")

    def test_unsafe_local_name_is_rejected(self):
        with self.assertRaises(music_module.MusicError):
            self.provider.resolve({"kind": "local", "file": "../secret.mp3"})

    def test_online_source_needs_a_music_path(self):
        with self.assertRaises(music_module.MusicError):
            self.provider.resolve({"kind": "online", "url": "http://evil/x"})


class AudioHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config_module.create_config(
            Path(self.tmp.name) / "config.json")
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.config["audio_runtime"] = str(Path(self.tmp.name) / "runtime")
        self.engine = Engine(self.config,
                             state_file=str(Path(self.tmp.name) / "s.json"))
        self.token = self.config["token"]
        handler = server_module.make_handler(self.engine, self.token)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.engine.close()
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def get(self, path, token=None):
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), response.headers

    def post(self, path, body, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), response.headers

    def last_command(self, kind):
        return [c for c in self.engine.pending(0) if c["type"] == kind][-1]

    # -- tests -------------------------------------------------------------

    def test_library_endpoint_lists_builtin_tracks(self):
        _, body, _ = self.get("/api/music/library", self.token)
        items = json.loads(body)["items"]
        self.assertGreaterEqual(len(items), 4)
        self.assertEqual(items[0]["kind"], "builtin")

    def test_test_clip_is_cached_queued_and_served(self):
        _, body, _ = self.post("/api/music/play", {"kind": "test"}, self.token)
        result = json.loads(body)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["title"], "试音提示")
        command = self.last_command("audio")
        self.assertEqual(command["title"], "试音提示")
        self.assertTrue(command["url"].startswith("/api/audio/"))
        code, clip, headers = self.get(command["url"], self.token)
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Content-Type"), "audio/wav")
        self.assertEqual(clip[:4], b"RIFF")
        self.assertEqual(len(clip), result["bytes"])

    def test_audio_endpoint_requires_the_token(self):
        self.post("/api/music/play", {"kind": "test"}, self.token)
        url = self.last_command("audio")["url"]
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(url)
        self.assertEqual(caught.exception.code, 401)

    def test_audio_endpoint_refuses_other_files(self):
        self.post("/api/music/play", {"kind": "test"}, self.token)
        name = self.last_command("audio")["url"].rsplit("/", 1)[-1]
        for bad in ("..%2F..%2Fconfig.json", name[:-4] + ".png", "x.wav"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get("/api/audio/" + bad, self.token)
            self.assertEqual(caught.exception.code, 404, bad)

    def test_play_sets_the_status_and_ack_closes_it(self):
        self.post("/api/music/play", {"kind": "test"}, self.token)
        _, body, _ = self.get("/api/status", self.token)
        self.assertEqual(json.loads(body)["audio"]["state"], "playing")
        self.engine.tts_errors = 3
        self.post("/api/event", {"type": "playback_done", "source": "audio"},
                  self.token)
        _, body, _ = self.get("/api/status", self.token)
        self.assertEqual(json.loads(body)["audio"]["state"], "idle")
        # An audio acknowledgement must not touch the TTS failure count.
        self.assertEqual(self.engine.tts_errors, 3)

    def test_audio_error_is_reported_without_a_tts_error(self):
        self.post("/api/music/play", {"kind": "test"}, self.token)
        self.post("/api/event", {"type": "playback_error", "source": "audio"},
                  self.token)
        _, body, _ = self.get("/api/status", self.token)
        self.assertEqual(json.loads(body)["audio"]["state"], "error")

    def test_stop_queues_a_device_command(self):
        self.post("/api/music/play", {"kind": "test"}, self.token)
        _, body, _ = self.post("/api/music/stop", {}, self.token)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.last_command("stop_audio")["type"], "stop_audio")
        _, body, _ = self.get("/api/status", self.token)
        self.assertEqual(json.loads(body)["audio"]["state"], "idle")

    def test_unplayable_online_song_fails_loudly_and_queues_nothing(self):
        error = urllib.error.HTTPError("http://neko/x", 404, "Not Found", {}, None)
        before = len(self.engine.pending(0))
        with mock.patch.object(music_module.MusicProvider, "_fetch",
                               side_effect=error):
            _, body, _ = self.post(
                "/api/music/play",
                {"kind": "online", "url": "/api/music/play/netease/1",
                 "name": "晴天"}, self.token)
        result = json.loads(body)
        self.assertFalse(result["ok"])
        self.assertIn("404", result["error"])
        self.assertEqual(len(self.engine.pending(0)), before)

    def test_builtin_track_from_a_fake_upstream_is_playable(self):
        mp3 = b"ID3" + b"\x00" * 4096
        with mock.patch.object(music_module.MusicProvider, "_fetch",
                               return_value=mp3):
            _, body, _ = self.post(
                "/api/music/play",
                {"kind": "builtin", "file": "Prelude.mp3"}, self.token)
        result = json.loads(body)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["title"], "Prelude")
        self.assertEqual(self.last_command("audio")["title"], "Prelude")

    def test_search_returns_descriptors_without_touching_the_device(self):
        payload = json.dumps({"success": True, "data": [
            {"name": "晴天", "artist": "周杰伦",
             "url": "/api/music/play/netease/1"}]}).encode("utf-8")
        with mock.patch.object(music_module.MusicProvider, "_fetch",
                               return_value=payload):
            _, body, _ = self.get("/api/music/search?q=%E6%99%B4%E5%A4%A9",
                                  self.token)
        items = json.loads(body)["items"]
        self.assertEqual(items[0]["kind"], "online")
        self.assertEqual(items[0]["name"], "晴天")

    # -- multiple folders, paging and catgirl shuffle ----------------------

    def _seed_folders(self):
        root = Path(self.tmp.name)
        folders = []
        for name, count in (("musicA", 2), ("musicB", 1)):
            folder = root / name
            folder.mkdir(parents=True, exist_ok=True)
            for i in range(count):
                # Include the folder name so two folders never produce the
                # same displayed title.
                (folder / ("%s - %s第%d首.mp3" % (name, name, i))).write_bytes(
                    b"ID3" + b"\x00" * 64)
            folders.append(str(folder))
        self.post("/api/music/folders", {"folders": folders}, self.token)
        current = {}
        for _ in range(300):
            current = json.loads(
                self.get("/api/music/folders", self.token)[1])
            if current["index"]["state"] == "ready":
                break
            time.sleep(0.01)
        return folders, current

    def test_multiple_folders_are_indexed_but_never_listed_in_full(self):
        folders, state = self._seed_folders()
        self.assertEqual(len(state["folders"]), 2)
        self.assertEqual(state["index"]["count"], 3)
        _, body, _ = self.get(
            "/api/music/library?q=&offset=0&limit=2", self.token)
        page = json.loads(body)
        # 4 built-ins + at most 2 local rows, never the whole collection.
        self.assertLessEqual(len(page["items"]), 6)
        self.assertEqual(page["limit"], 2)
        self.assertIn("index", page)

    def test_shuffle_picks_a_local_song_and_continues_after_playback_done(self):
        self._seed_folders()
        _, body, _ = self.post("/api/music/shuffle",
                               {"enabled": True}, self.token)
        result = json.loads(body)
        self.assertTrue(result["enabled"], result)
        self.assertTrue(result.get("playing"), result)
        first = [c for c in self.engine.pending(0) if c["type"] == "audio"]
        self.assertEqual(len(first), 1)
        self.assertIn("猫娘", first[-1]["note"])
        self.assertTrue(first[-1]["url"].startswith("/api/audio/"))
        self.post("/api/event",
                  {"type": "playback_done", "source": "audio"}, self.token)
        following = []
        for _ in range(200):
            following = [c for c in self.engine.pending(0)
                         if c["type"] == "audio"]
            if len(following) >= 2:
                break
            time.sleep(0.02)
        self.assertGreaterEqual(len(following), 2)
        self.assertNotEqual(following[0]["title"], following[1]["title"])

    def test_a_user_stop_does_not_start_the_next_song(self):
        self._seed_folders()
        self.post("/api/music/shuffle", {"enabled": True}, self.token)
        before = len([c for c in self.engine.pending(0)
                      if c["type"] == "audio"])
        self.post("/api/event",
                  {"type": "playback_stopped", "source": "audio"}, self.token)
        time.sleep(0.3)
        after = len([c for c in self.engine.pending(0)
                     if c["type"] == "audio"])
        self.assertEqual(after, before)

    def test_turning_shuffle_off_stops_the_continuation(self):
        self._seed_folders()
        self.post("/api/music/shuffle", {"enabled": True}, self.token)
        _, body, _ = self.post("/api/music/shuffle",
                               {"enabled": False}, self.token)
        self.assertFalse(json.loads(body)["enabled"])
        before = len([c for c in self.engine.pending(0)
                      if c["type"] == "audio"])
        self.post("/api/event",
                  {"type": "playback_done", "source": "audio"}, self.token)
        time.sleep(0.3)
        self.assertEqual(len([c for c in self.engine.pending(0)
                              if c["type"] == "audio"]), before)

    def test_next_button_changes_the_song(self):
        self._seed_folders()
        _, body, _ = self.post("/api/music/shuffle", {"next": True}, self.token)
        result = json.loads(body)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len([c for c in self.engine.pending(0)
                              if c["type"] == "audio"]), 1)

    def test_local_tracks_are_streamed_through_a_symlink(self):
        self._seed_folders()
        _, body, _ = self.get(
            "/api/music/library?q=&offset=0&limit=5", self.token)
        page = json.loads(body)
        local = [x for x in page["items"] if x["kind"] == "local"][0]
        _, body, _ = self.post("/api/music/play", {"item": local}, self.token)
        self.assertTrue(json.loads(body)["ok"])
        url = self.last_command("audio")["url"]
        cache = audio_module.AudioCache(
            Path(self.config["audio_runtime"]) / "audio")
        self.assertTrue(cache.path_for(url.rsplit("/", 1)[-1]).is_symlink())

    def test_search_local_returns_only_a_bounded_page_of_matches(self):
        self._seed_folders()
        _, body, _ = self.get("/api/music/search-local?q=musicA",
                              self.token)
        result = json.loads(body)
        self.assertTrue(result["items"], result)
        self.assertTrue(all(x["kind"] == "local" for x in result["items"]))
        self.assertLessEqual(len(result["items"]), 30)
        self.assertEqual(result["total"], 2)
        _, body, _ = self.get("/api/music/search-local?q=", self.token)
        self.assertEqual(json.loads(body)["items"], [])

    def test_big_or_hires_local_track_is_transcoded(self):
        self._seed_folders()
        _, body, _ = self.get(
            "/api/music/library?q=&offset=0&limit=5", self.token)
        local = [x for x in json.loads(body)["items"]
                 if x["kind"] == "local"][0]
        clip = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 64
        with mock.patch.object(music_module.MusicProvider,
                               "prepare_local_clip",
                               return_value=(clip, ".m4a")):
            _, body, _ = self.post("/api/music/play",
                                   {"item": local}, self.token)
        result = json.loads(body)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["transcoded"])
        self.assertTrue(self.last_command("audio")["url"].endswith(".m4a"))

    def test_a_failed_transcode_falls_back_to_the_original_file(self):
        self._seed_folders()
        _, body, _ = self.get(
            "/api/music/library?q=&offset=0&limit=5", self.token)
        local = [x for x in json.loads(body)["items"]
                 if x["kind"] == "local"][0]
        with mock.patch.object(music_module.MusicProvider,
                               "prepare_local_clip",
                               side_effect=music_module.MusicError(
                                   "afconvert 挂了")):
            _, body, _ = self.post("/api/music/play",
                                   {"item": local}, self.token)
        result = json.loads(body)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["transcoded"])
        self.assertIn("afconvert", result["transcode_error"])

    def test_cover_lyrics_and_note_travel_with_the_command(self):
        image = b"\xff\xd8\xff" + b"\x00" * 200
        lyric = json.dumps({"lrc": {"lyric":
                           "[00:01.00]第一句\n[00:03.00]第二句"}}).encode()
        fake_audio = b"ID3" + b"\x00" * 100
        with mock.patch.object(music_module.MusicProvider, "_fetch",
                               return_value=fake_audio), \
                mock.patch.object(music_module.MusicProvider, "_fetch_url",
                                  side_effect=[image, lyric]):
            _, body, _ = self.post("/api/music/play", {
                "kind": "online", "url": "/api/music/play/netease/123",
                "name": "晴天", "artist": "周杰伦",
                "cover": "https://p2.music.126.net/x.jpg",
                "note": "猫娘随手挑的"}, self.token)
        result = json.loads(body)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["has_cover"])
        self.assertEqual(result["lyric_lines"], 2)
        command = self.last_command("audio")
        self.assertEqual(command["note"], "猫娘随手挑的")
        self.assertTrue(command["cover"].startswith("/api/art/"))
        self.assertEqual(len(command["lyrics"]), 2)
        self.assertEqual(command["lyrics"][0]["t"], 1000)
        code, art, headers = self.get(command["cover"], self.token)
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Content-Type"), "image/jpeg")
        self.assertEqual(art[:3], b"\xff\xd8\xff")


    def test_browse_endpoint_lists_one_level_of_this_computer(self):
        folder = Path(self.tmp.name) / "browse"
        (folder / "子目录").mkdir(parents=True)
        (folder / "一首歌.mp3").write_bytes(b"ID3" + b"\x00" * 16)
        self.post("/api/music/folders", {"folders": [str(folder)]}, self.token)
        path = "/api/music/browse?path=" + urllib.parse.quote(str(folder))
        _, body, _ = self.get(path, self.token)
        result = json.loads(body)
        self.assertTrue(result["ok"], result)
        self.assertEqual([d["name"] for d in result["dirs"]], ["子目录"])
        self.assertEqual(result["tracks"], 1)
        self.assertEqual(result["scan_depth"], music_module.SCAN_DEPTH)

    def test_browse_endpoint_requires_an_absolute_path(self):
        _, body, _ = self.get(
            "/api/music/browse?path=" + urllib.parse.quote("相对路径"),
            self.token)
        self.assertFalse(json.loads(body)["ok"])

    def test_netease_account_endpoints(self):
        state = {"logged": False}

        def fake(_auth, path, method="GET", payload=None):
            if path == "/api/auth/cookies/save":
                state["logged"] = True
                return {"success": True, "message": "已保存"}
            if path == "/api/auth/cookies/netease" and method == "DELETE":
                state["logged"] = False
                return {"success": True}
            if path == "/api/auth/cookies/status":
                return {"success": True, "data": {"netease": {
                    "has_cookies": state["logged"],
                    "cookies_count": 1 if state["logged"] else 0,
                    "credential_state":
                        "ready" if state["logged"] else "missing"}}}
            self.fail("unexpected upstream call: " + path)

        with mock.patch.object(netease_module.NeteaseAuth, "_call", fake):
            _, body, _ = self.get("/api/music/netease", self.token)
            self.assertFalse(json.loads(body)["logged_in"])
            _, body, _ = self.post("/api/music/netease/login",
                                   {"cookie": "MUSIC_U=abc"}, self.token)
            result = json.loads(body)
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["logged_in"])
            _, body, _ = self.post("/api/music/netease/logout", {}, self.token)
            result = json.loads(body)
            self.assertTrue(result["ok"], result)
            self.assertFalse(result["logged_in"])

    def test_netease_login_rejects_an_empty_cookie(self):
        _, body, _ = self.post("/api/music/netease/login",
                               {"cookie": ""}, self.token)
        result = json.loads(body)
        self.assertFalse(result["ok"])
        self.assertIn("粘贴", result["error"])


class MediaTagsTest(unittest.TestCase):
    """Artwork and lyrics are parsed only for the track about to be played."""

    @staticmethod
    def _id3(frames):
        body = b"".join(fid + struct.pack(">I", len(data)) + b"\x00\x00" + data
                        for fid, data in frames)
        return b"ID3\x03\x00\x00" + bytes([
            (len(body) >> 21) & 0x7F, (len(body) >> 14) & 0x7F,
            (len(body) >> 7) & 0x7F, len(body) & 0x7F]) + body

    def test_lrc_timestamps_become_milliseconds(self):
        lines = music_module.parse_lrc(
            "[ti:标题]\n[00:12.34]第一行\n[01:02.500]第二行\n纯文字")
        self.assertEqual(lines, [
            {"t": 12340, "text": "第一行"},
            {"t": 62500, "text": "第二行"}])

    def test_lrc_is_bounded(self):
        text = "\n".join("[00:01.00]行%d" % i for i in range(1000))
        self.assertEqual(len(music_module.parse_lrc(text)),
                         music_module.MAX_LYRIC_LINES)

    def test_extract_apic_from_id3v23(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
        body = b"\x00image/jpeg\x00\x03\x00" + jpeg
        tag = self._id3([(b"APIC", body)])
        art = music_module.extract_apic(tag)
        self.assertIsNotNone(art)
        self.assertEqual(art[1], ".jpg")
        self.assertEqual(art[0][:3], b"\xff\xd8\xff")

    def test_extract_uslt_embedded_lyrics(self):
        # encoding 0 is latin-1, so keep the sample ASCII.
        body = b"\x00eng\x00[00:01.00]hello"
        tag = self._id3([(b"USLT", body)])
        self.assertEqual(music_module.extract_uslt(tag),
                         "[00:01.00]hello")

    def test_a_broken_tag_returns_nothing(self):
        self.assertIsNone(music_module.extract_apic(b"not an mp3"))
        self.assertIsNone(music_module.extract_uslt(b"ID3\x03\x00"))

    def test_online_lyrics_come_from_the_time_axis(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = music_module.MusicProvider(
                {"neko": {"url": "ws://127.0.0.1:48911"}},
                runtime_root=folder)
            payload = json.dumps({"lrc": {"lyric":
                                 "[00:05.00]一句歌词"}}).encode()
            with mock.patch.object(music_module.MusicProvider, "_fetch_url",
                                   return_value=payload):
                lines = provider.lyrics(
                    {"kind": "online", "url": "/api/music/play/netease/42"})
            self.assertEqual(lines, [{"t": 5000, "text": "一句歌词"}])


class LocalIndexTest(unittest.TestCase):
    """Tens of thousands of tracks: index names only, page everything."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = self.root / "A"
        self.b = self.root / "B"
        for folder in (self.a, self.b):
            folder.mkdir()
        for i in range(60):
            (self.a / ("歌手%d - 歌%d.mp3" % (i, i))).write_bytes(
                b"ID3" + b"\x00" * 16)
        (self.b / "另一首.flac").write_bytes(b"fLaC" + b"\x00" * 16)
        self.provider = music_module.MusicProvider(
            {"neko": {"url": "ws://127.0.0.1:48911"}},
            runtime_root=self.root / "runtime")

    def tearDown(self):
        self.tmp.cleanup()

    def _ready(self):
        self.provider.set_folders([str(self.a), str(self.b)])
        for _ in range(300):
            if self.provider.index.status()["state"] == "ready":
                return
            time.sleep(0.01)
        self.fail("index scan did not finish")

    def test_both_folders_are_indexed(self):
        self._ready()
        state = self.provider.index.status()
        self.assertEqual(state["count"], 61)
        self.assertEqual(len(state["folders"]), 2)
        page = self.provider.library_page("", 0, 10)
        self.assertEqual(len(page["items"]), 14)  # 4 built-ins + 10 local
        self.assertTrue(page["has_more"])
        self.assertEqual(page["offset"], 10)

    def test_keyword_filter_is_bounded(self):
        self._ready()
        page = self.provider.library_page("歌1", 0, 50)
        self.assertTrue(all("歌1" in x["name"] or "歌1" in x["file"]
                            for x in page["items"] if x["kind"] == "local"))
        self.assertLessEqual(len(page["items"]), 54)

    def test_random_avoids_the_recent_picks(self):
        self._ready()
        seen = set()
        for _ in range(10):
            item = self.provider.index.random(seen)
            self.assertNotIn(item["id"], seen)
            seen.add(item["id"])
        self.assertEqual(len(seen), 10)

    def test_path_for_rejects_escape_and_unknown_root(self):
        self._ready()
        with self.assertRaises(music_module.MusicError):
            self.provider.index.path_for({"file": "../secret.mp3",
                                          "root": str(self.a)})
        with self.assertRaises(music_module.MusicError):
            self.provider.index.path_for({"file": "x.mp3",
                                          "root": "/etc"})

    def test_index_survives_a_restart(self):
        self._ready()
        again = music_module.MusicProvider(
            {"neko": {"url": "ws://127.0.0.1:48911"}},
            runtime_root=self.root / "runtime")
        self.assertEqual(again.index.status()["count"], 61)


class ScanDepthTest(unittest.TestCase):
    """A chosen folder is walked five levels down, never six."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.provider = music_module.MusicProvider(
            {"neko": {"url": "ws://127.0.0.1:48911"}},
            runtime_root=self.root / "runtime")

    def tearDown(self):
        self.tmp.cleanup()

    def test_fifth_level_is_indexed_and_sixth_is_not(self):
        base = self.root / "music"
        base.mkdir()
        for depth in range(1, 7):
            folder = base.joinpath(*("L%d" % i for i in range(1, depth + 1)))
            folder.mkdir(parents=True, exist_ok=True)
            (folder / ("level%d.mp3" % depth)).write_bytes(
                b"ID3" + b"\x00" * 16)
        self.provider.set_folders([str(base)])
        for _ in range(300):
            if self.provider.index.status()["state"] == "ready":
                break
            time.sleep(0.01)
        rows, total, _ = self.provider.index.query("", 0, 50)
        self.assertEqual(total, 5)
        self.assertEqual(self.provider.index.status()["scan_depth"],
                         music_module.SCAN_DEPTH)
        self.assertTrue(any("L5" in row["file"] for row in rows))
        self.assertFalse(any("L6" in row["file"] for row in rows))


class FolderPickerTest(unittest.TestCase):
    """The console browses this computer one directory level at a time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_browse_lists_subfolders_and_counts_tracks(self):
        (self.root / "子目录A").mkdir()
        (self.root / "子目录B").mkdir()
        (self.root / ".隐藏目录").mkdir()
        (self.root / "一首歌.mp3").write_bytes(b"ID3")
        result = music_module.browse_directory(str(self.root), [self.root])
        self.assertTrue(result["ok"])
        self.assertEqual([d["name"] for d in result["dirs"]],
                         ["子目录A", "子目录B"])
        self.assertEqual(result["tracks"], 1)
        self.assertEqual(result["scan_depth"], music_module.SCAN_DEPTH)

    def test_browse_rejects_relative_and_system_paths(self):
        for bad in ("相对路径", "/etc"):
            with self.assertRaises(music_module.MusicError):
                music_module.browse_directory(bad, [self.root])

    def test_native_picker_is_unavailable_off_macos(self):
        with mock.patch.object(music_module.sys, "platform", "linux"):
            with self.assertRaises(music_module.MusicError):
                music_module.pick_folder_native()


class NeteaseAuthTest(unittest.TestCase):
    """The account is stored upstream, never in the bridge."""

    def setUp(self):
        self.auth = netease_module.NeteaseAuth("http://127.0.0.1:48911")

    def test_normalize_accepts_bare_and_full_cookie(self):
        self.assertEqual(self.auth.normalize("  abc123  "), "MUSIC_U=abc123")
        self.assertEqual(self.auth.normalize("MUSIC_U=abc; ntese=1"),
                         "MUSIC_U=abc; ntese=1")

    def test_normalize_rejects_empty_and_foreign_cookies(self):
        for bad in ("", "   ", "foo=bar", "a\nb"):
            with self.assertRaises(netease_module.NeteaseError):
                self.auth.normalize(bad)

    def test_status_is_offline_when_upstream_is_down(self):
        with mock.patch.object(self.auth, "_call",
                               side_effect=netease_module.NeteaseError("x")):
            result = self.auth.status()
        self.assertFalse(result["online"])
        self.assertFalse(result["logged_in"])
        self.assertIn("cookies_login", result["credential_page"])

    def test_login_then_logout(self):
        state = {"logged": False}

        def fake(path, method="GET", payload=None):
            if path == "/api/auth/cookies/save":
                state["logged"] = True
                return {"success": True, "message": "已保存"}
            if path == "/api/auth/cookies/netease" and method == "DELETE":
                state["logged"] = False
                return {"success": True}
            if path == "/api/auth/cookies/status":
                return {"success": True, "data": {"netease": {
                    "has_cookies": state["logged"],
                    "cookies_count": 1 if state["logged"] else 0}}}
            self.fail("unexpected call " + path)

        with mock.patch.object(self.auth, "_call", fake):
            logged = self.auth.login("abc123")
            self.assertTrue(logged["ok"])
            self.assertTrue(logged["logged_in"])
            cleared = self.auth.logout()
            self.assertTrue(cleared["ok"])
            self.assertFalse(cleared["logged_in"])


JPEG_SAMPLE = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"


def _id3_tag(frames):
    body = b"".join(
        fid + struct.pack(">I", len(data)) + b"\x00\x00" + data
        for fid, data in frames)
    return b"ID3\x03\x00\x00" + bytes([
        (len(body) >> 21) & 0x7F, (len(body) >> 14) & 0x7F,
        (len(body) >> 7) & 0x7F, len(body) & 0x7F]) + body


def _flac_picture(image=JPEG_SAMPLE):
    mime, desc = b"image/jpeg", b"front"
    return (struct.pack(">I", 3) + struct.pack(">I", len(mime)) + mime +
            struct.pack(">I", len(desc)) + desc +
            struct.pack(">IIII", 500, 500, 24, 0) +
            struct.pack(">I", len(image)) + image)


def _vorbis_block(pairs):
    vendor = b"test"
    raw = (struct.pack("<I", len(vendor)) + vendor +
           struct.pack("<I", len(pairs)))
    for key, value in pairs:
        entry = key.encode() + b"=" + value.encode()
        raw += struct.pack("<I", len(entry)) + entry
    return raw


def _flac_block(kind, body, last=False):
    return (bytes([kind | (0x80 if last else 0)]) +
            len(body).to_bytes(3, "big") + body)


def _ogg_page(payload):
    return (b"OggS" + b"\x00\x00" + b"\x00" * 8 + b"\x00" * 12 +
            b"\x01" + bytes([len(payload)]) + payload)


def _mp4_box(kind, payload):
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _mp4_data(payload):
    return _mp4_box(b"data", b"\x00" * 8 + payload)


class EmbeddedMetadataTest(unittest.TestCase):
    """The track file's own tags win, in each common container."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def _assert_art_and_lyrics(self, path, word):
        result = music_module.embedded_metadata(path)
        self.assertIsNotNone(result["art"], path.suffix)
        self.assertEqual(result["art"][0], JPEG_SAMPLE)
        self.assertEqual(result["art"][1], ".jpg")
        self.assertIn(word, result["lyrics"] or "")
        return result

    def test_id3_art_and_utf8_lyrics(self):
        apic = b"\x00image/jpeg\x00\x03\x00" + JPEG_SAMPLE
        uslt = b"\x00eng\x00[00:01.00]" + "内嵌歌词".encode("utf-8")
        path = self._file("a.mp3", _id3_tag([(b"APIC", apic),
                                             (b"USLT", uslt)]))
        self._assert_art_and_lyrics(path, "内嵌歌词")

    def test_flac_picture_and_vorbis_lyrics(self):
        comments = _vorbis_block([
            ("LYRICS", "[00:02.00]FLAC歌词"),
            ("METADATA_BLOCK_PICTURE",
             base64.b64encode(_flac_picture()).decode())])
        data = (b"fLaC" + _flac_block(0, b"\x00" * 34) +
                _flac_block(4, comments) +
                _flac_block(6, _flac_picture(), True))
        self._assert_art_and_lyrics(self._file("a.flac", data), "FLAC歌词")

    def test_ogg_comment_packet(self):
        comments = _vorbis_block([
            ("LYRICS", "[00:03.00]OGG歌词"),
            ("METADATA_BLOCK_PICTURE",
             base64.b64encode(_flac_picture()).decode())])
        data = (_ogg_page(b"\x01vorbis" + b"\x00" * 20) +
                _ogg_page(b"\x03vorbis" + comments))
        self._assert_art_and_lyrics(self._file("a.ogg", data), "OGG歌词")

    def _mp4_with_moov(self, moov):
        return _mp4_box(b"ftyp", b"M4A \x00\x00\x00\x00") + moov

    def test_mp4_covr_and_lyrics(self):
        ilst = (_mp4_box(b"covr", _mp4_data(JPEG_SAMPLE)) +
                _mp4_box(b"\xa9lyr",
                         _mp4_data("[00:04.00]MP4歌词".encode("utf-8"))))
        moov = _mp4_box(b"moov", _mp4_box(
            b"udta", _mp4_box(b"meta", b"\x00" * 4 +
                              _mp4_box(b"ilst", ilst))))
        self._assert_art_and_lyrics(
            self._file("a.m4a", self._mp4_with_moov(moov)), "MP4歌词")

    def test_mp4_moov_at_the_end_of_a_large_file(self):
        ilst = (_mp4_box(b"covr", _mp4_data(JPEG_SAMPLE)) +
                _mp4_box(b"\xa9lyr",
                         _mp4_data("[00:05.00]尾部歌词".encode("utf-8"))))
        moov = _mp4_box(b"moov", _mp4_box(
            b"udta", _mp4_box(b"meta", b"\x00" * 4 +
                              _mp4_box(b"ilst", ilst))))
        padding = b"\x00" * (2 * 1024 * 1024)
        data = _mp4_box(b"ftyp", b"M4A \x00\x00\x00\x00") + padding + moov
        self._assert_art_and_lyrics(
            self._file("big.m4a", data), "尾部歌词")

    def test_a_broken_file_returns_nothing(self):
        path = self._file("broken.mp3", b"not an audio file")
        self.assertEqual(music_module.embedded_metadata(path),
                         {"art": None, "lyrics": None,
                          "title": None, "artist": None})

    def test_embedded_wins_over_sibling_cover_and_lrc(self):
        provider = music_module.MusicProvider(
            {"neko": {"url": "ws://127.0.0.1:48911"}},
            runtime_root=self.root / "runtime")
        apic = b"\x00image/jpeg\x00\x03\x00" + JPEG_SAMPLE
        uslt = b"\x00eng\x00[00:01.00]" + "自带的一句".encode("utf-8")
        track = self._file("歌.mp3", _id3_tag([(b"APIC", apic),
                                               (b"USLT", uslt)]))
        (self.root / "cover.jpg").write_bytes(b"\xff\xd8\xffSIBLING")
        (self.root / "歌.lrc").write_text("[00:09.00]外置的一句",
                                          encoding="utf-8")
        art = provider.artwork({"kind": "local"}, track)
        lines = provider.lyrics({"kind": "local"}, track)
        self.assertEqual(art[0], JPEG_SAMPLE)
        self.assertEqual(lines, [{"t": 1000, "text": "自带的一句"}])

    def test_siblings_are_the_fallback_when_the_file_has_no_tags(self):
        provider = music_module.MusicProvider(
            {"neko": {"url": "ws://127.0.0.1:48911"}},
            runtime_root=self.root / "runtime")
        track = self._file("歌.mp3", b"\xff\xfb" + b"\x00" * 64)
        (self.root / "cover.jpg").write_bytes(b"\xff\xd8\xffSIBLING")
        (self.root / "歌.lrc").write_text("[00:09.00]外置的一句",
                                          encoding="utf-8")
        art = provider.artwork({"kind": "local"}, track)
        lines = provider.lyrics({"kind": "local"}, track)
        self.assertEqual(art[0], b"\xff\xd8\xffSIBLING")
        self.assertEqual(lines, [{"t": 9000, "text": "外置的一句"}])


class PlaybackCompatibilityTest(unittest.TestCase):
    """24-bit / 96 kHz / >24 MB FLAC is converted before it reaches the speaker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _flac(path, rate, bits, channels=2, total_size=None):
        packed = ((rate << 44) | ((channels - 1) << 41) |
                  ((bits - 1) << 36))
        body = b"\x00" * 10 + packed.to_bytes(8, "big") + b"\x00" * 16
        head = b"fLaC" + bytes([0]) + len(body).to_bytes(3, "big") + body
        with open(path, "wb") as handle:
            handle.write(head)
            if total_size:
                handle.truncate(total_size)
        return path

    def test_streaminfo_is_read_from_the_header(self):
        path = self._flac(self.root / "a.flac", 96000, 24)
        self.assertEqual(music_module.flac_streaminfo(path), (96000, 2, 24))
        other = self.root / "b.mp3"
        other.write_bytes(b"ID3" + b"\x00" * 32)
        self.assertIsNone(music_module.flac_streaminfo(other))

    def test_only_hires_or_oversized_files_are_converted(self):
        self.assertFalse(music_module.needs_transcode(
            self._flac(self.root / "cd.flac", 44100, 16)))
        self.assertTrue(music_module.needs_transcode(
            self._flac(self.root / "hi.flac", 44100, 24)))
        self.assertTrue(music_module.needs_transcode(
            self._flac(self.root / "96k.flac", 96000, 16)))
        self.assertTrue(music_module.needs_transcode(
            self._flac(self.root / "big.flac", 44100, 16,
                       total_size=25 * 1024 * 1024)))

    def test_provider_streams_a_compatible_file_untouched(self):
        provider = music_module.MusicProvider(
            {"neko": {"url": "ws://127.0.0.1:48911"}},
            runtime_root=self.root / "runtime")
        path = self._flac(self.root / "cd.flac", 44100, 16)
        data, suffix = provider.prepare_local_clip(path)
        self.assertIsNone(data)
        self.assertEqual(suffix, ".flac")

    def test_afconvert_output_is_a_small_m4a_and_leaves_no_temp_file(self):
        path = self._flac(self.root / "hi.flac", 44100, 24)

        def fake_run(command, **kwargs):
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypM4A ")
            return music_module.subprocess.CompletedProcess(
                command, 0, b"", b"")

        with mock.patch.object(music_module.subprocess, "run", fake_run):
            provider = music_module.MusicProvider(
                {"neko": {"url": "ws://127.0.0.1:48911"}},
                runtime_root=self.root / "runtime")
            data, suffix = provider.prepare_local_clip(path)
        self.assertEqual(suffix, ".m4a")
        self.assertEqual(data[:8], b"\x00\x00\x00\x18ftyp")
        self.assertEqual(list((self.root / "runtime" / "tmp").glob("*")), [])

    @unittest.skipUnless(Path(music_module.AFCONVERT).exists(),
                         "macOS afconvert is needed for the real file check")
    def test_real_transcode_only_reads_the_original_file(self):
        """The user's music library is never renamed / rewritten / appended to."""
        library = self.root / "my-music-library"
        library.mkdir()
        source = library / "a-song.wav"
        with wave.open(str(source), "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(44100)
            out.writeframes(b"\x00" * 4 * 4410)   # 50 ms of silence

        before = source.read_bytes()
        stat_before = source.stat()
        data, suffix = music_module.transcode_for_device(
            source, self.root / "runtime" / "tmp")

        self.assertEqual(suffix, ".m4a")
        self.assertEqual(data[4:8], b"ftyp")
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(source.stat().st_mtime_ns, stat_before.st_mtime_ns)
        self.assertEqual([p.name for p in library.iterdir()], ["a-song.wav"])
        self.assertEqual(list((self.root / "runtime" / "tmp").glob("*")), [])


if __name__ == "__main__":
    unittest.main()
