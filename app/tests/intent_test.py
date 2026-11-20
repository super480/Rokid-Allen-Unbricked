"""Intent parsing: which assistant the user selected."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import intent  # noqa: E402
import config  # noqa: E402


class IntentTest(unittest.TestCase):
    def test_select_hermes_by_name(self):
        self.assertEqual(intent.parse_intent("找小赫"), ("select", "hermes"))
        self.assertEqual(intent.parse_intent("小赫"), ("select", "hermes"))
        self.assertEqual(intent.parse_intent("找Hermes"), ("select", "hermes"))

    def test_select_hermes_with_prefixes(self):
        for prefix in ("找", "切换到", "切换成", "切换", "叫", "请找"):
            self.assertEqual(
                intent.parse_intent(prefix + "小赫"), ("select", "hermes"),
                "prefix %s should select hermes" % prefix)

    def test_wake_word_is_stripped(self):
        self.assertEqual(intent.parse_intent("若琪找小赫"), ("select", "hermes"))
        self.assertEqual(intent.parse_intent("若琪，小赫"), ("select", "hermes"))

    def test_punctuation_and_case_are_ignored(self):
        self.assertEqual(intent.parse_intent("找，HERMES"), ("select", "hermes"))
        self.assertEqual(intent.parse_intent("找 小赫。"), ("select", "hermes"))

    def test_exit(self):
        self.assertEqual(intent.parse_intent("退出助手"), ("exit", None))
        self.assertEqual(intent.parse_intent("退出小赫"), ("exit", None))
        self.assertEqual(intent.parse_intent("结束对话"), ("exit", None))

    def test_repeat_and_stop(self):
        self.assertEqual(intent.parse_intent("再说一遍"), ("repeat", None))
        self.assertEqual(intent.parse_intent("别说了"), ("stop", None))

    def test_plain_sentence_is_a_command(self):
        action, argument = intent.parse_intent("帮我看看今天的日程")
        self.assertEqual(action, "command")
        self.assertEqual(argument, "帮我看看今天的日程")

    def test_disabled_assistant_is_known_but_not_selectable(self):
        # Which assistant is switched off changes as the project grows, so
        # the test switches one off itself rather than naming it. What
        # matters is the rule, not the state of the registry today.
        with mock.patch.dict(config.REGISTRY["hermes"], {"enabled": False}):
            action, argument = intent.parse_intent("找小赫")
            self.assertEqual(action, "select")
            self.assertEqual(argument, "hermes")
            self.assertIn("hermes", intent.known_agents())
            self.assertFalse(intent.select_enabled("hermes"))
        for key in ("hermes", "dsh"):
            self.assertTrue(intent.select_enabled(key))

    def test_normalize_removes_punctuation(self):
        self.assertEqual(intent.normalize("小赫。"), "小赫")

    def test_the_second_assistant_can_be_selected(self):
        for said in ("找大肥鱼", "找大非鱼", "找肥鱼", "找飞鱼", "找dsh",
                     "找deepseek", "叫大肥鱼", "切换到大肥鱼"):
            self.assertEqual(intent.parse_intent(said), ("select", "dsh"),
                             "%s should select dsh" % said)

    def test_misheard_names_still_select_the_assistant(self):
        # The recogniser returns a different character for the same
        # syllable: "小赫" arrives as "小鹤" or "小贺".
        for said in ("找小鹤", "找小贺", "找小喝", "找小盒", "找小合"):
            self.assertEqual(intent.parse_intent(said), ("select", "hermes"),
                             "%s should select hermes" % said)

    def test_misheard_names_work_with_every_prefix(self):
        for prefix in ("找", "切换到", "切换成", "切换", "叫", "请找"):
            self.assertEqual(intent.parse_intent(prefix + "小鹤"),
                             ("select", "hermes"))

    def test_misheard_names_without_a_prefix(self):
        self.assertEqual(intent.parse_intent("小鹤"), ("select", "hermes"))

    def test_misheard_exit_words_still_exit(self):
        self.assertEqual(intent.parse_intent("退出小鹤"), ("exit", None))

    def test_fold_only_touches_name_characters(self):
        self.assertEqual(intent.fold("找小鹤"), "找小赫")
        # Ordinary wording is untouched, so commands still reach the
        # assistant as the user said them.
        self.assertEqual(intent.fold("帮我看看今天的日程"), "帮我看看今天的日程")

    def test_other_assistants_survive_their_own_mishearings(self):
        self.assertEqual(intent.parse_intent("找大非鱼"), ("select", "dsh"))
        self.assertEqual(intent.parse_intent("找老扣"), ("select", "codex"))
        self.assertEqual(intent.parse_intent("找巴帝"), ("select", "workbuddy"))

class WakeWordMatchTest(unittest.TestCase):
    """The label printed by the wake engine maps back to an assistant.

    The engine logs the vt_word it matched, not the sentence that follows.
    Those labels are the assistant's name (Chinese or English), and the
    factory's own word is a pipe-separated list of aliases.
    """

    SPELLINGS = {
        "dsh": ["大肥鱼", "dsh", "deepseek", "肥鱼", "飞鱼"],
        "workbuddy": ["巴蒂", "workbuddy", "buddy", "巴帝"],
        "codex": ["GPT", "codex", "gpt", "chatgpt", "老寇"],
        "hermes": ["Hermes", "hermes", "小赫", "小和"],
    }
    PINYINS = {
        "dsh": "da4fei2yu2", "workbuddy": "ba1di4",
        "codex": "ji4pi4ti4", "hermes": "xiao3he4",
    }

    def route(self, word):
        return intent.wake_word_agent(word, self.SPELLINGS, self.PINYINS)

    def test_every_registered_name_routes_to_its_assistant(self):
        for word, agent in [("大肥鱼", "dsh"), ("巴蒂", "workbuddy"),
                            ("GPT", "codex"), ("Hermes", "hermes"),
                            ("小赫", "hermes"), ("gpt", "codex")]:
            self.assertEqual(self.route(word), agent, word)

    def test_a_homophone_of_a_registered_alias_routes(self):
        self.assertEqual(self.route("小鹤"), "hermes")
        self.assertEqual(self.route("巴帝"), "workbuddy")

    def test_the_phonetic_spelling_routes_too(self):
        self.assertEqual(self.route("da4fei2yu2"), "dsh")
        self.assertEqual(self.route("xiao3he4"), "hermes")

    def test_a_chinese_transcription_of_an_english_label_routes(self):
        # "GPT" is registered as ji4pi4ti4; the engine may print the
        # Chinese characters it heard instead of the label "GPT".
        self.assertEqual(self.route("鸡屁踢"), "codex")
        self.assertEqual(self.route("基皮提"), "codex")

    def test_the_factory_word_and_unknown_words_do_not_route(self):
        for word in ("若琪|洛奇", "若琪", "没事了", "你好", ""):
            self.assertIsNone(self.route(word), word)

    def test_the_feature_being_off_routes_nothing(self):
        self.assertIsNone(intent.wake_word_agent("大肥鱼", {}, self.PINYINS))

    def test_a_pipe_separated_label_still_finds_its_assistant(self):
        self.assertEqual(self.route("大肥鱼|大非鱼"), "dsh")

    def test_a_wrapped_label_still_finds_its_assistant(self):
        self.assertEqual(self.route("唤醒词:巴蒂"), "workbuddy")


if __name__ == "__main__":
    unittest.main()
