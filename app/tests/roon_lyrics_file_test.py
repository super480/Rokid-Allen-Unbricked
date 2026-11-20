"""Roon-side lyrics: read the LRC inside the very file the Core is playing.

The Roon zone only reports title/artist/album, never a path. The Core's
library is often mounted on this Mac but outside the speaker's play queue
folders, so the old lookup never opened the file and showed "暂无歌词".
"""
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
import music as music_module  # noqa: E402

SHEET = ("[00:00.00]作词 : 黄俊郎\n"
         "[00:16.35]呜啦啦啦火车笛\n"
         "[00:18.16]随着奔腾的马蹄\n")


def write_fake_flac(path, lyrics=SHEET, title="牛仔很忙", artist="周杰伦"):
    """A tiny FLAC header whose Vorbis comment holds the embedded LRC."""
    entries = [b"TITLE=" + title.encode("utf-8"),
               b"ARTIST=" + artist.encode("utf-8"),
               b"LYRICS=" + lyrics.encode("utf-8")]
    vendor = b"rokid-test"
    body = struct.pack("<I", len(vendor)) + vendor
    body += struct.pack("<I", len(entries))
    for entry in entries:
        body += struct.pack("<I", len(entry)) + entry
    head = b"fLaC"
    head += b"\x00" + (34).to_bytes(3, "big") + b"\x00" * 34
    head += b"\x84" + len(body).to_bytes(3, "big") + body
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head)


class RoonLibraryLyricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # macOS resolves /var to /private/var; the settings code does too.
        self.root = Path(self.tmp.name).resolve()
        self.library = self.root / "ROON"
        self.album = self.library / "周杰伦" / "周杰伦 - 2007 - 我很忙"
        self.track = self.album / "周杰伦 - 牛仔很忙.flac"
        write_fake_flac(self.track)
        (self.root / "play").mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.provider = music_module.MusicProvider(
            {}, local_root=self.root / "no-such-play-folder",
            runtime_root=self.runtime)

    def test_artist_album_title_locate_the_original_file(self):
        found = music_module.find_lyric_files(
            "牛仔很忙", "周杰伦", "我很忙", [str(self.library)])
        self.assertEqual(found, [self.track])

    def test_embedded_lrc_is_returned_even_without_an_index_entry(self):
        lines = music_module.lyrics_for_track(
            self.provider, "牛仔很忙", "周杰伦", album="我很忙",
            roots=[str(self.library)], online=False)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1], {"t": 16350, "text": "呜啦啦啦火车笛"})
        self.assertEqual(self.provider.index.status()["count"], 0)

    def test_works_without_the_album_name_too(self):
        found = music_module.find_lyric_files(
            "牛仔很忙", "周杰伦", "", [str(self.library)])
        self.assertEqual(found, [self.track])

    def test_a_different_track_in_the_same_album_is_not_used(self):
        write_fake_flac(self.album / "周杰伦 - 青花瓷.flac",
                        lyrics="[00:01.00]素胚勾勒出青花\n", title="青花瓷")
        found = music_module.find_lyric_files(
            "牛仔很忙", "周杰伦", "我很忙", [str(self.library)])
        self.assertEqual(found, [self.track])

    def test_no_artist_folder_never_walks_the_whole_share(self):
        deep = self.library / "杂项" / "A" / "B" / "周杰伦 - 牛仔很忙.flac"
        write_fake_flac(deep)
        found = music_module.find_lyric_files(
            "牛仔很忙", "一个不存在的歌手", "", [str(self.library)])
        self.assertEqual(found, [])

    def test_unmounted_or_missing_root_is_quiet(self):
        self.assertEqual(music_module.find_lyric_files(
            "牛仔很忙", "周杰伦", "我很忙",
            [str(self.root / "not-mounted")]), [])

    def test_provider_roots_are_used_when_no_explicit_roots(self):
        self.provider.set_lyrics_roots([str(self.library)])
        lines = music_module.lyrics_for_track(
            self.provider, "牛仔很忙", "周杰伦", album="我很忙",
            online=False)
        self.assertEqual(len(lines), 3)


class LyricsRootsSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # macOS resolves /var to /private/var; the settings code does too.
        self.root = Path(self.tmp.name).resolve()
        (self.root / "ROON" / "TANK").mkdir(parents=True)
        (self.root / "ROON" / "周杰伦").mkdir()
        self.settings = music_module.MusicSettings(self.root / "runtime")

    def test_auto_guess_is_the_parent_of_each_play_folder(self):
        self.settings.save_folders([str(self.root / "ROON" / "TANK")])
        self.assertEqual(self.settings.lyrics_roots(), [str(self.root / "ROON")])

    def test_saving_play_folders_keeps_the_lyrics_root(self):
        self.settings.save_lyrics_roots([str(self.root / "ROON" / "周杰伦")])
        self.settings.save_folders([str(self.root / "ROON" / "TANK")])
        self.assertEqual(self.settings.lyrics_roots(),
                         [str(self.root / "ROON" / "周杰伦")])

    def test_empty_list_disables_and_none_restores_the_guess(self):
        self.settings.save_folders([str(self.root / "ROON" / "TANK")])
        self.settings.save_lyrics_roots([])
        self.assertEqual(self.settings.lyrics_roots(), [])
        self.settings.save_lyrics_roots(None)
        self.assertEqual(self.settings.lyrics_roots(), [str(self.root / "ROON")])

    def test_a_missing_folder_is_rejected(self):
        with self.assertRaises(music_module.MusicError):
            self.settings.save_lyrics_roots([str(self.root / "gone")])


if __name__ == "__main__":
    unittest.main()
