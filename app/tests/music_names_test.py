"""The now-playing screen shows the singer stored inside the music file.

A ripped CD often names its files ``01. 五月天 - 拥抱.flac``. Guessing the
artist from that filename used to put "01." in front of the singer, so
local playback now reads the file's own TITLE / ARTIST tag instead. The
index still keeps filenames only: a library with tens of thousands of
tracks must never be opened in bulk.
"""
import json
import struct
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
import music as music_module  # noqa: E402
import server as server_module  # noqa: E402
from engine import Engine  # noqa: E402


def _vorbis(pairs):
    vendor = b"test"
    raw = (struct.pack("<I", len(vendor)) + vendor +
           struct.pack("<I", len(pairs)))
    for key, value in pairs:
        entry = key.encode() + b"=" + value.encode()
        raw += struct.pack("<I", len(entry)) + entry
    return raw


def _block(kind, body, last=False):
    return (bytes([kind | (0x80 if last else 0)]) +
            len(body).to_bytes(3, "big") + body)


def _flac(pairs):
    return (b"fLaC" + _block(0, b"\x00" * 34) +
            _block(4, _vorbis(pairs), True))


def _id3(frames, version=3):
    body = b"".join(
        fid + struct.pack(">I", len(data)) + b"\x00\x00" + data
        for fid, data in frames)
    return (b"ID3" + bytes([version, 0, 0]) + bytes([
        (len(body) >> 21) & 0x7F, (len(body) >> 14) & 0x7F,
        (len(body) >> 7) & 0x7F, len(body) & 0x7F]) + body)


def _text_frame(value):
    return b"\x00" + value.encode("utf-8")


def _syncsafe(number):
    return bytes([(number >> 21) & 0x7F, (number >> 14) & 0x7F,
                  (number >> 7) & 0x7F, number & 0x7F])


def _id3_v22(frames):
    """Real v2.2 layout: three-letter id + three-byte size + body."""
    body = b"".join(fid + len(data).to_bytes(3, "big") + data
                    for fid, data in frames)
    return b"ID3\x02\x00\x00" + _syncsafe(len(body)) + body


class TagNamesTest(unittest.TestCase):
    """One file per format, parsed without any third-party library."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def _tags(self, name, data):
        return music_module.embedded_metadata(self._file(name, data))

    def test_flac_vorbis_artist_and_title(self):
        tags = self._tags("01. 五月天 - 拥抱.flac",
                          _flac([("TITLE", "拥抱"),
                                 ("ARTIST", "五月天"),
                                 ("ALBUMARTIST", "五月天")]))
        self.assertEqual(tags["title"], "拥抱")
        self.assertEqual(tags["artist"], "五月天")

    def test_album_artist_is_only_a_fallback(self):
        tags = self._tags("01. Tank - 曙光.flac",
                          _flac([("TITLE", "曙光"),
                                 ("ALBUMARTIST", "Tank")]))
        self.assertEqual(tags["artist"], "Tank")
        both = self._tags("compilation.flac",
                          _flac([("ARTIST", "迪克牛仔"),
                                 ("ALBUMARTIST", "动力火车 & 迪克牛仔")]))
        self.assertEqual(both["artist"], "迪克牛仔")

    def test_ogg_comment_packet(self):
        comments = _vorbis([("TITLE", "序"), ("ARTIST", "动力火车")])
        payload = b"\x03vorbis" + comments
        page = (b"OggS" + b"\x00\x00" + b"\x00" * 8 + b"\x00" * 12 +
                b"\x01" + bytes([len(payload)]) + payload)
        tags = self._tags("a.ogg", page)
        self.assertEqual(tags["title"], "序")
        self.assertEqual(tags["artist"], "动力火车")

    def test_id3v2_text_frames(self):
        tags = self._tags("a.mp3", _id3([
            (b"TIT2", _text_frame("疯狂世界")),
            (b"TPE1", _text_frame("五月天")),
            (b"TPE2", _text_frame("滚石唱片"))]))
        self.assertEqual(tags["title"], "疯狂世界")
        self.assertEqual(tags["artist"], "五月天")

    def test_id3v2_album_artist_fallback(self):
        tags = self._tags("a.mp3", _id3([
            (b"TIT2", _text_frame("歌")),
            (b"TPE2", _text_frame("Tank"))]))
        self.assertEqual(tags["artist"], "Tank")

    def test_id3v2_2_three_letter_frames(self):
        data = _id3_v22([(b"TT2", _text_frame("拥抱")),
                         (b"TP1", _text_frame("五月天"))])
        tags = self._tags("v22.mp3", data)
        self.assertEqual(tags["title"], "拥抱")
        self.assertEqual(tags["artist"], "五月天")

    def test_id3v1_tail_is_a_fallback(self):
        tag = (b"TAG" + "拥抱".encode("gbk").ljust(30, b"\x00") +
               "五月天".encode("gbk").ljust(30, b"\x00") +
               b"\x00" * 65)
        tags = self._tags("old.mp3", b"\xff\xfb" + b"\x00" * 64 + tag)
        self.assertEqual(tags["title"], "拥抱")
        self.assertEqual(tags["artist"], "五月天")

    def test_mp4_nam_and_art(self):
        def box(kind, payload):
            return struct.pack(">I", 8 + len(payload)) + kind + payload

        def data(payload):
            return box(b"data", b"\x00" * 8 + payload)

        ilst = (box(b"\xa9nam", data("曙光".encode("utf-8"))) +
                box(b"\xa9ART", data("Tank".encode("utf-8"))) +
                box(b"aART", data("华研".encode("utf-8"))))
        moov = box(b"moov", box(b"udta", box(
            b"meta", b"\x00" * 4 + box(b"ilst", ilst))))
        tags = self._tags("a.m4a", box(b"ftyp", b"M4A \x00\x00\x00\x00") +
                          moov)
        self.assertEqual(tags["title"], "曙光")
        self.assertEqual(tags["artist"], "Tank")

    def test_broken_file_has_no_names(self):
        tags = self._tags("broken.mp3", b"not an audio file")
        self.assertEqual(tags, {"art": None, "lyrics": None,
                                "title": None, "artist": None})


class GuessedNamesTest(unittest.TestCase):
    """Filenames are only a fallback, and never keep the CD track number."""

    def test_track_numbers_are_stripped(self):
        cases = {
            "01. 五月天 - 拥抱": ("拥抱", "五月天"),
            "02. Tank - 曙光": ("曙光", "Tank"),
            "13. 五月天 - 候鸟": ("候鸟", "五月天"),
            "01 五月天 - 拥抱": ("拥抱", "五月天"),
            "01 疯狂世界": ("疯狂世界", "本地音乐"),
        }
        for stem, expected in cases.items():
            self.assertEqual(music_module._guess_names(stem), expected, stem)

    def test_real_names_that_start_with_a_number_survive(self):
        self.assertEqual(music_module._guess_names("2NE1 - 某首歌"),
                         ("某首歌", "2NE1"))
        self.assertEqual(music_module._strip_track_prefix("3D 乐队"),
                         "3D 乐队")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _provider(self):
        return music_module.MusicProvider(
            {"neko": {"url": "ws://127.0.0.1:48911"}},
            runtime_root=self.root / "runtime")

    def test_metadata_prefers_the_tag_over_the_filename(self):
        path = self.root / "01. 五月天 - 拥抱.flac"
        path.write_bytes(_flac([("TITLE", "拥抱"), ("ARTIST", "五月天")]))
        item = {"kind": "local", "name": "拥抱", "artist": "01. 五月天"}
        self.assertEqual(self._provider().metadata(item, path),
                         {"title": "拥抱", "artist": "五月天"})

    def test_untagged_file_falls_back_to_a_clean_filename_guess(self):
        path = self.root / "02. Tank - 曙光.flac"
        path.write_bytes(b"not an audio file")
        item = {"kind": "local", "name": "曙光", "artist": "02. Tank"}
        self.assertEqual(self._provider().metadata(item, path),
                         {"title": "曙光", "artist": "Tank"})

    def test_an_old_index_row_is_clean_without_being_rewritten(self):
        provider = self._provider()
        provider.index.entries = [{
            "kind": "local", "id": "local:/tmp/01. 五月天 - 拥抱.flac",
            "name": "拥抱", "artist": "01. 五月天",
            "file": "01. 五月天 - 拥抱.flac", "root": "/tmp",
            "size": 1024, "suffix": ".flac"}]
        rows, total, _ = provider.index.query("", 0, 10)
        self.assertEqual(rows[0]["artist"], "五月天")
        self.assertEqual(rows[0]["file"], "01. 五月天 - 拥抱.flac")
        # The stored row keeps its original text for substring matching.
        self.assertEqual(provider.index.entries[0]["artist"], "01. 五月天")
        self.assertEqual(total, 1)


class PlaybackNamesApiTest(unittest.TestCase):
    """The actual audio command carries the tag, not the filename prefix."""

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

    def post(self, path, body):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def get(self, path):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            headers={"Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def test_play_command_shows_the_singer_from_the_file(self):
        folder = Path(self.tmp.name) / "songs"
        folder.mkdir()
        (folder / "01. 五月天 - 拥抱.flac").write_bytes(
            _flac([("TITLE", "拥抱"), ("ARTIST", "五月天")]))
        self.post("/api/music/folders", {"folders": [str(folder)]})
        for _ in range(300):
            if self.get("/api/music/folders")["index"]["state"] == "ready":
                break
            time.sleep(0.01)
        else:
            self.fail("the local index never became ready")
        page = self.get("/api/music/library?q=&offset=0&limit=5")
        item = [x for x in page["items"] if x["kind"] == "local"][0]
        # A synthetic FLAC is not real audio; skip the transcode decision.
        original = music_module.embedded_metadata
        with mock.patch.object(music_module.MusicProvider,
                               "prepare_local_clip",
                               return_value=(b"fLaC" + b"\x00" * 64,
                                             ".flac")), \
                mock.patch.object(music_module, "embedded_metadata",
                                  wraps=original) as tag_reader:
            result = self.post("/api/music/play", {"item": item})
        self.assertGreaterEqual(tag_reader.call_count, 1)
        # Names, cover and lyrics share one read of the file.
        self.assertEqual(tag_reader.call_count, 1)
        self.assertTrue(result["ok"], result)
        audio = [c for c in self.engine.pending(0)
                 if c["type"] == "audio"][-1]
        self.assertEqual(audio["artist"], "五月天")
        self.assertEqual(audio["title"], "拥抱")
        self.assertNotIn("01.", audio["artist"])


if __name__ == "__main__":
    unittest.main()
