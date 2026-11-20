"""Ear-only mode's second half: the Mac reads the answers.

Host-side only. The runner is faked, so nothing here makes a sound: the
tests check what *would* have been said and, more importantly, that the
speaker's microphone cannot hand the Mac's own voice back to us as a user
turn.
"""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
import mac_speech  # noqa: E402
from engine import ECHO_TAIL, Engine  # noqa: E402


class FakeStdin:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, chunk):
        self.data += chunk

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, gate=None):
        self.stdin = FakeStdin()
        self.gate = gate
        self.terminated = False

    def wait(self):
        if self.gate is not None:
            self.gate.wait(5)
        return 0

    def terminate(self):
        self.terminated = True
        if self.gate is not None:
            self.gate.set()


class FakeRunner:
    """Records what would have been spoken instead of speaking it."""

    def __init__(self, gate=None):
        self.calls = []
        self.gate = gate

    def __call__(self):
        process = FakeProcess(self.gate)
        self.calls.append(process)
        return process

    @property
    def texts(self):
        return [call.stdin.data.decode("utf-8") for call in self.calls]


def make_engine(root):
    config = config_module.create_config(root / "config.json")
    config["_root"] = str(root)
    config["_path"] = str(root / "config.json")
    config["roon_bridge_url"] = "http://127.0.0.1:1"
    return config, Engine(config, state_file=str(root / "state.json"))


def wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class MacVoiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config, self.engine = make_engine(self.root)
        # A line stays "being spoken" until the test releases it, so the echo
        # window is observable instead of closing microseconds later.
        self.gate = threading.Event()
        self.runner = FakeRunner(self.gate)
        self.engine.mac_speaker = mac_speech.MacSpeech(runner=self.runner)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def of_type(self, kind):
        return [c for c in self.engine.pending(0) if c["type"] == kind]

    def enable_wake(self, word="猫娘"):
        self.engine.config["wake"] = {
            "enabled": True,
            "agents": {"neko": {"enabled": True, "pinyin": "mao niang",
                                "aliases": [word]}},
        }

    # -- the two halves of the mode ------------------------------------------

    def test_ear_mode_speaks_on_the_mac_and_keeps_the_speaker_silent(self):
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("这是一条测试回复。", "neko", "rid-1")
        self.assertEqual(self.of_type("speak"), [])
        self.assertEqual(len(self.of_type("display")), 1)
        self.assertTrue(wait_until(lambda: len(self.runner.calls) == 1))
        self.assertTrue(self.runner.texts[0])

    def test_with_ear_mode_off_the_mac_stays_quiet(self):
        self.engine.deliver_speech("这是一条测试回复。", "neko", "rid-2")
        self.assertEqual(len(self.of_type("speak")), 1)
        self.assertEqual(self.runner.calls, [])

    def test_leaving_the_mode_cuts_the_mac_line_off(self):
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("这是一条测试回复。", "neko", "rid-3")
        self.assertTrue(wait_until(lambda: self.runner.calls))
        self.engine.set_neko_ear_only(enabled=False)
        self.assertTrue(self.runner.calls[0].terminated)

    def test_a_muted_mac_keeps_the_speaker_silent_too(self):
        self.engine.config.setdefault("neko", {})["mac_voice"] = {"enabled": False}
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("这是一条测试回复。", "neko", "rid-4")
        self.assertEqual(self.of_type("speak"), [])
        self.assertEqual(self.runner.calls, [])

    # -- what keeps the mode usable in one room ------------------------------

    def test_the_mac_line_is_not_heard_as_a_user_turn(self):
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("今天外面在下雨，记得带伞。", "neko", "rid-5")
        heard = self.engine.recent_speeches[-1]["text"]
        answer = self.engine.handle_event({"type": "asr_final", "text": heard,
                                           "request_id": "rid-5-echo"})
        self.assertEqual(answer["intent"], "echo")
        self.assertTrue(answer.get("duplicate"))

    def test_a_wake_word_inside_the_mac_line_does_not_switch_agent(self):
        self.enable_wake()
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("猫娘在这儿呢。", "neko", "rid-6")
        answer = self.engine.handle_event({"type": "wake_word", "word": "猫娘"})
        self.assertEqual(answer["intent"], "wake_echo")
        self.assertIsNone(self.engine.selected)

    def test_wake_words_are_dodged_out_of_the_spoken_line(self):
        self.enable_wake()
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("猫娘在这儿呢，GPT 也在。", "neko", "rid-7")
        spoken = self.engine.mac_speaker.last_text
        self.assertNotIn("猫娘", spoken)
        self.assertIn("人家", spoken)

    def test_dodging_can_be_switched_off(self):
        self.enable_wake()
        self.engine.config["neko"]["mac_voice"] = {"dodge": False}
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("猫娘在这儿呢。", "neko", "rid-8")
        self.assertIn("猫娘", self.engine.mac_speaker.last_text)

    def test_the_factory_name_is_dodged_too(self):
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("若琪的屏幕亮着呢。", "neko", "rid-15")
        spoken = self.engine.mac_speaker.last_text
        self.assertNotIn("若琪", spoken)
        self.assertEqual(self.engine.mac_voice_settings()["dodge_extra"],
                         ["若琪", "洛奇"])

    def test_the_mac_window_survives_a_device_playback_report(self):
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("这是一条测试回复。", "neko", "rid-9")
        entry = self.engine.recent_speeches[-1]
        self.assertTrue(entry.get("mac"))
        self.engine.handle_event({"type": "playback_done"})
        self.assertIsNone(entry.get("ended"))
        self.assertTrue(self.engine.wake_during_speech())

    def test_a_bare_wake_from_the_mac_voice_does_not_light_the_screen(self):
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("今天外面在下雨，记得带伞。", "neko", "rid-13")
        before = len(self.of_type("state"))
        answer = self.engine.handle_event({"type": "wake"})
        self.assertEqual(answer["intent"], "wake_echo")
        self.assertEqual(len(self.of_type("state")), before)

    def test_a_two_word_answer_cannot_start_a_loop(self):
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("嗯。", "neko", "rid-14")
        heard = self.engine.recent_speeches[-1]["text"]
        answer = self.engine.handle_event({"type": "asr_final", "text": heard,
                                           "request_id": "rid-14-echo"})
        self.assertEqual(answer["intent"], "echo")

    def test_the_window_closes_when_the_mac_line_ends(self):
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("今天外面在下雨，记得带伞。", "neko", "rid-10")
        entry = self.engine.recent_speeches[-1]
        self.gate.set()
        self.assertTrue(wait_until(lambda: entry.get("ended") is not None))
        ended = entry["ended"]
        self.assertFalse(self.engine.wake_during_speech(now=ended + ECHO_TAIL + 1))
        self.assertFalse(self.engine.is_echo("今天外面在下雨，记得带伞。",
                                            now=ended + ECHO_TAIL + 1))

    def test_a_new_line_replaces_the_previous_one(self):
        self.engine.set_neko_ear_only(enabled=True)
        self.engine.deliver_speech("第一句。", "neko", "rid-11")
        self.assertTrue(wait_until(lambda: self.runner.calls))
        self.engine.deliver_speech("第二句。", "neko", "rid-12")
        self.assertTrue(self.runner.calls[0].terminated)
        self.assertTrue(wait_until(lambda: len(self.runner.calls) == 2))


class MacSpeechTest(unittest.TestCase):
    def test_dodge_replaces_longest_words_first_case_insensitively(self):
        text = mac_speech.dodge("GPT 和 gpt，还有猫娘。", ["猫娘", "GPT"], "人家")
        self.assertEqual(text, "人家 和 人家，还有人家。")

    def test_dodge_leaves_text_without_wake_words_alone(self):
        self.assertEqual(mac_speech.dodge("今天天气不错。", ["猫娘"]),
                         "今天天气不错。")

    def test_estimate_grows_with_length_and_is_never_instant(self):
        speaker = mac_speech.MacSpeech(runner=FakeRunner())
        self.assertGreaterEqual(speaker.estimate("嗯"), 1.5)
        self.assertGreater(speaker.estimate("一" * 100),
                           speaker.estimate("一" * 10))

    def test_the_rate_is_clamped_to_something_say_accepts(self):
        self.assertEqual(mac_speech.MacSpeech(rate=5000).rate, 400)
        self.assertEqual(mac_speech.MacSpeech(rate="fast").rate,
                         mac_speech.DEFAULT_RATE)

    def test_an_empty_line_is_not_spoken(self):
        runner = FakeRunner()
        self.assertIsNone(mac_speech.MacSpeech(runner=runner).speak("   "))
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
