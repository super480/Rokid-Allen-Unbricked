"""Lyrics for a Roon track: local library first, Netease only on a title hit.

Roon's now_playing payload carries no words. The lookup must never fetch a
different song's lyric just because the artist matched, otherwise the words
scroll at the wrong times.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
import music as music_module  # noqa: E402

TITLE = "To Forgive 宥"
ARTIST = "林宥嘉"
SHEET = [{"t": 0, "text": "第一句"}, {"t": 9000, "text": "第二句"}]


class FakeProvider:
    def __init__(self):
        self.local_results = {}
        self.online_results = {}
        self.sheets = {}
        self.online_calls = []
        self.local_calls = []

    def search_local(self, query, limit=30):
        self.local_calls.append(query)
        rows = self.local_results.get(query, [])
        return {"items": rows[:limit], "total": len(rows),
                "has_more": len(rows) > limit}

    def search(self, query, limit=8):
        self.online_calls.append(query)
        return self.online_results.get(query, [])[:limit]

    def resolve_local_path(self, item):
        return Path("/fake") / str(item.get("file") or "song.flac")

    def read_tags(self, path):
        return {}

    def lyrics(self, item, local_path=None, tags=None):
        return self.sheets.get(str(item.get("id") or ""), [])


class RoonLyricsTest(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()

    def find(self, title=TITLE, artist=ARTIST, online=True):
        return music_module.lyrics_for_track(
            self.provider, title, artist, online=online)

    def test_local_title_hit_wins_without_touching_the_network(self):
        item = {"kind": "local", "id": "L1", "name": TITLE,
                "artist": ARTIST, "file": "a.flac"}
        self.provider.local_results[TITLE] = [item]
        self.provider.sheets["L1"] = SHEET
        self.assertEqual(self.find(), SHEET)
        self.assertFalse(self.provider.online_calls)

    def test_artist_only_local_hit_is_not_this_song(self):
        # A path/artist coincidence must not make us read another file.
        item = {"kind": "local", "id": "L2", "name": "全世界谁倾听你",
                "artist": ARTIST, "file": "林宥嘉/b.flac"}
        self.provider.local_results[TITLE] = [item]
        self.provider.sheets["L2"] = SHEET
        self.assertEqual(self.find(), [])
        self.assertEqual(len(self.provider.online_calls), 2)

    def test_netease_whole_title_hit_is_used(self):
        item = {"kind": "online", "id": "N1", "name": "To forgive 宥",
                "artist": "TIKER_T",
                "url": "/api/music/play/netease/2147699171"}
        self.provider.online_results[TITLE] = [item]
        self.provider.sheets["N1"] = SHEET
        self.assertEqual(self.find(), SHEET)

    def test_netease_wrong_version_is_rejected(self):
        # Real-world case: searching "林宥嘉 To Forgive 宥" returns The
        # Smashing Pumpkins' "To Forgive" and an unrelated 林宥嘉 song.
        wrong = {"kind": "online", "id": "N2", "name": "To Forgive",
                 "artist": "The Smashing Pumpkins",
                 "url": "/api/music/play/netease/22469969"}
        artist_only = {"kind": "online", "id": "N3",
                       "name": "全世界谁倾听你", "artist": ARTIST,
                       "url": "/api/music/play/netease/432509483"}
        self.provider.online_results[ARTIST + " " + TITLE] = [wrong, artist_only]
        self.provider.sheets["N2"] = SHEET
        self.provider.sheets["N3"] = SHEET
        self.assertEqual(self.find(), [])

    def test_no_title_returns_empty_and_does_no_work(self):
        self.assertEqual(self.find(title="  "), [])
        self.assertFalse(self.provider.local_calls)
        self.assertFalse(self.provider.online_calls)

    def test_online_can_be_disabled(self):
        self.assertEqual(self.find(online=False), [])
        self.assertFalse(self.provider.online_calls)

    def test_sheet_is_bounded(self):
        item = {"kind": "local", "id": "L1", "name": TITLE,
                "artist": ARTIST, "file": "a.flac"}
        self.provider.local_results[TITLE] = [item]
        self.provider.sheets["L1"] = [
            {"t": i * 1000, "text": "行%d" % i} for i in range(500)]
        self.assertEqual(len(self.find()), music_module.MAX_ROON_LYRIC_LINES)

    def test_a_broken_candidate_never_stops_the_lookup(self):
        bad = {"kind": "local", "id": "BAD", "name": TITLE,
               "artist": ARTIST, "file": "broken.flac"}
        good = {"kind": "local", "id": "GOOD", "name": TITLE,
                "artist": ARTIST, "file": "good.flac"}
        self.provider.local_results[TITLE] = [bad, good]
        self.provider.sheets["GOOD"] = SHEET
        original = self.provider.lyrics

        def flaky(item, local_path=None, tags=None):
            if item["id"] == "BAD":
                raise OSError("unreadable tag")
            return original(item, local_path, tags)

        self.provider.lyrics = flaky
        self.assertEqual(self.find(), SHEET)


if __name__ == "__main__":
    unittest.main()
