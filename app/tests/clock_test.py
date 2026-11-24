"""Tests for the standby clock host service (D050).

The device side (app/clock/) is a plain APK -- what lives here is the
voice entry, the face broadcast, and the console card plumbing. The
tests stub the engine and patch subprocess so no device is touched.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
HOST = ROOT / "host"
for entry in (str(ROOT), str(HOST)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from clock import (ACTION_SET_FACE, BUILTIN_KEYWORDS, ClockService,  # noqa: E402
                   is_clock_query)


class StubEngine:
    """config / log_transcript / _save_config only."""

    def __init__(self, device="test:5555"):
        self.config = {"device": device} if device else {}
        self.transcript = []
        self.saved = 0

    def log_transcript(self, *args):
        self.transcript.append(args)

    def _save_config(self):
        self.saved += 1


def make_service(enabled=True, device="test:5555", **settings):
    service = ClockService(StubEngine(device=device), config=settings)
    return service


class PhraseTest(unittest.TestCase):
    def test_builtin_keywords_hit(self):
        for phrase in ("换个表盘", "若琪，下一个表盘", "帮我切换表盘！",
                       "换个时钟样式", "换个时钟"):
            self.assertTrue(is_clock_query(phrase), phrase)

    def test_non_queries_pass(self):
        for phrase in ("今天天气怎么样", "现在几点", "放首歌", ""):
            self.assertFalse(is_clock_query(phrase), phrase)

    def test_custom_phrase_matches_after_normalisation(self):
        # The D044 normalisation: pasted with 「若琪，」 and punctuation,
        # the comma split drops the bare wake word (its key is empty) and
        # the recogniser delivers the clean remainder -- one key.
        service = make_service()
        service.set_voice_phrases("若琪，换个皮肤！")
        self.assertEqual(service.voice_phrases, ["换个皮肤！"])
        with patch.object(ClockService, "_broadcast") as broadcast:
            self.assertFalse(is_clock_query("换个皮肤"))
            result = service.handle_text("换个皮肤", "r1")
        self.assertTrue(result["switched"])
        self.assertEqual(broadcast.call_count, 1)

    def test_custom_phrase_limits_match_the_miniapps(self):
        service = make_service()
        service.set_voice_phrases([">" + "问" * 40])  # over 40 chars
        self.assertEqual(service.voice_phrases, [])
        service.set_voice_phrases(["问%d" % i for i in range(20)])
        self.assertEqual(len(service.voice_phrases), 12)
        result = service.set_voice_phrases([])
        self.assertEqual(result["phrases"], [])
        self.assertEqual(service.voice_phrases, [])


class VoiceDispatchTest(unittest.TestCase):
    def test_query_cycles_to_next_face(self):
        service = make_service()
        with patch.object(ClockService, "_broadcast",
                          return_value=True) as broadcast:
            result = service.handle_text("换个表盘", "r2")
        self.assertEqual(result, {"accepted": True, "intent": "clock",
                                  "switched": True})
        broadcast.assert_called_once_with("next")

    def test_unrelated_turn_passes_on(self):
        service = make_service()
        with patch.object(ClockService, "_broadcast") as broadcast:
            self.assertIsNone(service.handle_text("今天天气怎么样", "r3"))
        self.assertFalse(broadcast.called)

    def test_broadcast_command_shape(self):
        service = make_service()
        with patch("clock.subprocess.run") as run:
            run.return_value.stdout = "Broadcast completed: result=0"
            ok = service._broadcast("digital")
        self.assertTrue(ok)
        args = run.call_args[0][0]
        self.assertIn("shell", args)
        shell = args[-1]
        self.assertIn("am broadcast -a " + ACTION_SET_FACE, shell)
        self.assertIn("--es face digital", shell)

    def test_broadcast_without_device_is_a_noop(self):
        service = make_service(device="")
        with patch("clock.subprocess.run") as run:
            self.assertFalse(service._broadcast("grid"))
        self.assertFalse(run.called)


class ConsoleCardTest(unittest.TestCase):
    def test_status_shape(self):
        service = make_service()
        with patch.object(ClockService, "_read_device_face",
                          return_value="grid"):
            status = service.status()
        self.assertEqual(status["face"], "grid")
        self.assertEqual(status["faces"], ["grid", "digital", "orbit", "weekday", "minimal", "lagoon", "milk", "forest", "roman", "factory_sunset", "factory_fantasy", "factory_bluesky", "factory_jellyfish"])
        self.assertEqual(status["voice_phrases"], [])
        self.assertEqual(status["builtin_phrases"], list(BUILTIN_KEYWORDS))
        self.assertEqual(status["builtin_mode"], "keywords")

    def test_legacy_saved_face_list_keeps_custom_and_new_builtin(self):
        service = make_service(faces=["grid", "digital", "my-skin"])
        with patch.object(ClockService, "_read_device_face", return_value="orbit"):
            status = service.status()
        self.assertEqual(status["face"], "orbit")
        self.assertEqual(status["faces"],
                         ["grid", "digital", "orbit", "weekday", "minimal", "lagoon", "milk", "forest", "roman", "factory_sunset", "factory_fantasy", "factory_bluesky", "factory_jellyfish", "my-skin"])

    def test_set_face_persists_and_broadcasts(self):
        service = make_service()
        with patch.object(ClockService, "_broadcast",
                          return_value=True) as broadcast, \
                patch.object(ClockService, "_read_device_face",
                             return_value="digital"):
            result = service.set_face("digital")
        broadcast.assert_called_once_with("digital")
        self.assertEqual(service.settings["face"], "digital")
        self.assertEqual(result["face"], "digital")  # device read echoes

    def test_set_face_rejects_empty(self):
        service = make_service()
        with self.assertRaises(ValueError):
            service.set_face("  ")

    def test_set_voice_phrases_persists(self):
        service = make_service()
        with patch.object(service, "_persist") as persist:
            result = service.set_voice_phrases("换个皮肤，换个花样")
        self.assertEqual(result["phrases"], ["换个皮肤", "换个花样"])
        self.assertEqual(service.voice_phrases, ["换个皮肤", "换个花样"])
        self.assertTrue(persist.called)


if __name__ == "__main__":
    unittest.main()
