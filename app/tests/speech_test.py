"""Speech sanitising and clipping."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import speech  # noqa: E402


class SpeechTest(unittest.TestCase):
    def test_markdown_emphasis_is_removed(self):
        self.assertEqual(speech.to_speech("**粗体**"), "粗体。")

    def test_code_block_is_announced_not_read(self):
        out = speech.to_speech("看这段：\n```python\nprint(1)\n```\n结束")
        self.assertIn("（代码略）", out)
        self.assertNotIn("print", out)

    def test_links_are_announced_not_read(self):
        out = speech.to_speech("见 https://example.com/a")
        self.assertNotIn("https", out)
        self.assertIn("链接已省略", out)

    def test_headings_and_bullets_become_sentences(self):
        out = speech.to_speech("## 标题\n- 第一项\n- 第二项")
        self.assertNotIn("#", out)
        self.assertNotIn("-", out)
        self.assertIn("第一项", out)

    def test_clip_prefers_a_sentence_boundary(self):
        text = "短句。" * 10
        clipped = speech.clip(text, 11, tail="")
        # The cut lands on the last full stop inside the window.
        self.assertEqual(clipped, "短句。短句。短句。")

    def test_clip_appends_the_tail_when_truncated(self):
        clipped = speech.clip("一二三四五六七八九十" * 5, 10)
        self.assertIn("完整内容请查看电脑端", clipped)

    def test_clip_never_exceeds_the_limit_plus_the_tail(self):
        clipped = speech.clip("短句。" * 10, 12)
        self.assertLessEqual(len(clipped), 12 + len("完整内容请查看电脑端。"))

    def test_short_text_is_untouched(self):
        self.assertEqual(speech.clip("很短。", 400), "很短。")

    def test_summary_is_one_line(self):
        summary = speech.summary_of("第一行\n第二行\n第三行", 60)
        self.assertNotIn("\n", summary)


if __name__ == "__main__":
    unittest.main()
