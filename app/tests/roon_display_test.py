"""The Roon now-playing screen shows words, not the play clock.

The overlay cannot be captured with screencap on this device, so these
source-level assertions guard the one behaviour the user asked for: the
middle line under the cover must be a lyric line, never mm:ss.
"""
import unittest
from pathlib import Path

ANDROID = Path(__file__).resolve().parents[1] / "android" / "src" / "cn" / "rokid" / "bridge"


class RoonDisplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.view = (ANDROID / "NowPlayingView.java").read_text(encoding="utf-8")
        cls.service = (ANDROID / "BridgeService.java").read_text(encoding="utf-8")

    def test_no_clock_on_the_roon_screen(self):
        self.assertNotIn("mmss(", self.view)
        self.assertNotIn("String progress", self.view)

    def test_middle_line_uses_the_timed_lyric_index(self):
        self.assertIn("currentLine(position())", self.view)
        self.assertIn("暂无歌词", self.view)
        # The Roon branch must reuse the same sheet as local playback.
        self.assertGreaterEqual(self.view.count("currentLine(position())"), 2)

    def test_paused_state_moved_to_the_zone_line(self):
        self.assertIn("· 已暂停", self.view)

    def test_bridge_keeps_the_sheet_between_seek_refreshes(self):
        self.assertIn('command.has("lyrics")', self.service)
        self.assertIn("roonLyricTimes", self.service)
        self.assertIn("showNowPlaying(title, artist, roonLyricTimes,",
                      self.service)


if __name__ == "__main__":
    unittest.main()
