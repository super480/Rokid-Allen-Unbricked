"""Proactive chat for the fifth assistant (B plan, route R3).

The timer only submits a job to the single worker, so an unsolicited turn
can never overlap a question the user asked. Tests cover the gate, quiet
hours that cross midnight, settings validation, the console's test button,
and the single speech exit.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
import engine as engine_module  # noqa: E402
from engine import (Engine, PROACTIVE_PROMPT, PROACTIVE_MEMORY_TEXT,  # noqa: E402
                    hhmm_to_minutes, in_quiet_window)  # noqa: E402


class FakeNekoBackend:
    def __init__(self, answer="喵，忽然想到你了。"):
        self.answer = answer
        self.calls = []

    def run(self, text, request_id, on_progress=None, memory_text=None):
        self.calls.append((text, request_id, memory_text))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def cancel(self, request_id):
        return True

    def close(self):
        pass

    def health(self):
        return True, "ok"


class TimeHelpersTest(unittest.TestCase):
    def test_hhmm(self):
        self.assertEqual(hhmm_to_minutes("23:30"), 1410)
        self.assertEqual(hhmm_to_minutes("00:00"), 0)
        self.assertEqual(hhmm_to_minutes("24:00"), 0)   # midnight
        self.assertEqual(hhmm_to_minutes("11:00"), 660)
        self.assertEqual(hhmm_to_minutes("garbage", 7), 7)
        self.assertEqual(hhmm_to_minutes("25:00", -1), -1)
        self.assertEqual(hhmm_to_minutes("12:99", -1), -1)

    def test_quiet_window_same_day(self):
        self.assertTrue(in_quiet_window(0, 0, 660))      # 00:00
        self.assertTrue(in_quiet_window(659, 0, 660))    # 10:59
        self.assertFalse(in_quiet_window(660, 0, 660))   # 11:00
        self.assertFalse(in_quiet_window(720, 0, 660))   # 12:00

    def test_quiet_window_crosses_midnight(self):
        self.assertTrue(in_quiet_window(1410, 1380, 480))   # 23:30
        self.assertTrue(in_quiet_window(60, 1380, 480))     # 01:00
        self.assertFalse(in_quiet_window(600, 1380, 480))   # 10:00

    def test_equal_start_and_end_means_no_quiet(self):
        self.assertFalse(in_quiet_window(120, 600, 600))


class ProactiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.path = Path(self.tmp.name) / "config.json"
        self.config = config_module.create_config(self.path)
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.config["_path"] = str(self.path)   # let _save_config write
        self.engine = Engine(self.config, state_file=str(self.state))
        self.backend = FakeNekoBackend()
        self.engine.backend_for = lambda agent_id: self.backend
        self.engine._proactive_stop.set()   # no timer ticks during a test

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def _speak_commands(self):
        return [c for c in self.engine.pending(0) if c["type"] == "speak"]

    def test_defaults(self):
        s = self.engine.neko_proactive_settings()
        self.assertTrue(s["enabled"])
        self.assertEqual(s["interval_minutes"], 10)
        self.assertEqual(s["quiet_start"], "00:00")
        self.assertEqual(s["quiet_end"], "11:00")
        self.assertEqual(s["idle_seconds"], 120)

    def test_defaults_are_not_shared_with_DEFAULTS(self):
        self.engine.set_neko_proactive(interval_minutes=3)
        self.assertEqual(
            config_module.DEFAULTS["neko"]["proactive"]["interval_minutes"], 10)

    def test_settings_validate_and_save(self):
        s = self.engine.set_neko_proactive(
            enabled=False, interval_minutes=5,
            quiet_start="23:30", quiet_end="08:00")
        self.assertFalse(s["enabled"])
        self.assertEqual(s["interval_minutes"], 5)
        self.assertEqual(s["quiet_start_min"], 1410)
        self.assertEqual(s["quiet_end_min"], 480)
        written = json.loads(self.path.read_text())
        self.assertEqual(written["neko"]["proactive"]["quiet_start"], "23:30")
        with self.assertRaises(ValueError):
            self.engine.set_neko_proactive(quiet_start="9:3")
        with self.assertRaises(ValueError):
            self.engine.set_neko_proactive(interval_minutes=0)
        with self.assertRaises(ValueError):
            self.engine.set_neko_proactive(unknown=True)

    def test_gate_conditions(self):
        now = time.time()
        s = self.engine.neko_proactive_settings()
        self.engine.selected = "neko"
        # Pick a moment outside the default quiet window (12:00 local).
        s2 = dict(s, quiet_start="23:00", quiet_end="08:00")
        s2["quiet_start_min"], s2["quiet_end_min"] = 1380, 480
        noon = now - (time.localtime(now).tm_hour * 3600
                      + time.localtime(now).tm_min * 60) + 12 * 3600
        self.engine._proactive_last_at = noon - 3600
        self.engine.last_utterance = ("", noon - 3600)
        self.assertTrue(self.engine._proactive_gate(noon, s2))
        self.assertFalse(self.engine._proactive_gate(
            noon, dict(s2, enabled=False)))
        self.engine.selected = "dsh"
        self.assertFalse(self.engine._proactive_gate(noon, s2))
        self.engine.selected = "neko"
        self.engine.busy = True
        self.assertFalse(self.engine._proactive_gate(noon, s2))
        self.engine.busy = False
        self.engine.last_utterance = ("", noon - 1)
        self.assertFalse(self.engine._proactive_gate(noon, s2))  # idle cooldown
        self.engine.last_utterance = ("", now - 3600)
        self.engine._proactive_last_at = noon - 10
        self.assertFalse(self.engine._proactive_gate(noon, s2))  # interval
        self.engine._proactive_last_at = noon - 3600
        # Inside the default quiet window (00:30 local).
        quiet = now - (time.localtime(now).tm_hour * 3600
                       + time.localtime(now).tm_min * 60) + 30 * 60
        self.assertTrue(self.engine._quiet_now(s, quiet))

    def test_test_button_replays_last_line_without_touching_the_backend(self):
        # N.E.K.O. reads every answer aloud on the desktop client, so the
        # console's test button must not generate a new turn: it replays the
        # most recent catgirl line on the speaker and nothing else.
        self.engine.selected = "neko"
        self.engine.log_transcript("agent", "neko", "刚才看到一只橘猫。")
        self.engine.log_transcript("user", "neko", "在忙吗")
        result = self.engine.proactive_now()
        self.assertTrue(result["accepted"])
        self.assertTrue(result["request_id"].startswith("neko-replay-"))
        self.assertTrue(result["replay"])
        self.assertEqual(self.backend.calls, [])
        job = self.engine.jobs.get_nowait()
        self.assertTrue(job["replay"])
        self.assertEqual(job["agent"], "neko")
        self.assertEqual(job["text"], "刚才看到一只橘猫。")
        self.engine._run_replay(job)
        self.engine.jobs.task_done()
        speaks = self._speak_commands()
        self.assertEqual(len(speaks), 1)
        self.assertIn("橘猫", speaks[0]["text"])
        # Replaying never calls N.E.K.O., so the Mac catgirl stays silent.
        self.assertEqual(self.backend.calls, [])

    def test_prompt_no_longer_looks_like_a_music_refusal(self):
        # N.E.K.O. read "不要推荐歌曲" as the user declining music
        # recommendations and paused its suggestions for 20 minutes.
        for word in ("歌曲", "音乐", "推荐", "不要"):
            self.assertNotIn(word, PROACTIVE_PROMPT)
        self.assertIn("主动说一句", PROACTIVE_PROMPT)

    def test_proactive_turn_keeps_its_instruction_out_of_memory(self):
        self.engine.selected = "neko"
        self.engine._run_proactive({
            "text": PROACTIVE_PROMPT, "request_id": "neko-proactive-mem",
            "agent": "neko", "proactive": True})
        self.assertEqual(len(self.backend.calls), 1)
        text, rid, memory_text = self.backend.calls[0]
        self.assertEqual(text, PROACTIVE_PROMPT)       # model sees the prompt
        self.assertEqual(memory_text, PROACTIVE_MEMORY_TEXT)
        self.assertNotEqual(memory_text, PROACTIVE_PROMPT)
        self.assertNotIn("系统请求", memory_text)

    def test_fresh_button_submits_a_new_turn(self):
        self.engine.selected = "neko"
        result = self.engine.proactive_now("fresh")
        self.assertTrue(result["accepted"])
        self.assertTrue(result["request_id"].startswith("neko-proactive-"))
        self.assertNotIn("replay", result)
        job = self.engine.jobs.get_nowait()
        self.assertTrue(job["proactive"])
        self.assertEqual(job["text"], PROACTIVE_PROMPT)
        self.engine.jobs.task_done()

    def test_unknown_button_mode_is_rejected(self):
        self.engine.selected = "neko"
        with self.assertRaises(ValueError):
            self.engine.proactive_now("whatever")

    def test_test_button_with_no_history_says_so(self):
        self.engine.selected = "neko"
        self.assertEqual(self.engine.proactive_now()["reason"],
                         "no answer yet")
        self.assertEqual(self._speak_commands(), [])

    def test_test_button_ignores_gates_but_needs_neko_selected(self):
        self.engine.log_transcript("agent", "neko", "喵，忽然想到你了。")
        self.engine.selected = "dsh"
        self.assertEqual(self.engine.proactive_now()["reason"],
                         "neko not selected")
        self.engine.selected = "neko"
        self.engine.set_neko_proactive(enabled=False)
        self.assertTrue(self.engine.proactive_now()["accepted"])

    def test_automatic_submit_is_gated(self):
        self.engine.selected = "dsh"
        self.assertFalse(self.engine._submit_proactive()["accepted"])

    def test_run_proactive_speaks_through_the_single_exit(self):
        self.engine.selected = "neko"
        job = {"text": PROACTIVE_PROMPT, "request_id": "neko-proactive-test",
               "agent": "neko", "proactive": True}
        self.engine._run_proactive(job)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.backend.calls[0][0], PROACTIVE_PROMPT)
        speaks = self._speak_commands()
        self.assertEqual(len(speaks), 1)
        self.assertIn("忽然想到你", speaks[0]["text"])
        self.assertEqual(speaks[0]["agent"], "neko")
        self.assertFalse(self.engine.busy)

    def test_switching_away_before_execution_cancels_silently(self):
        self.engine.selected = "dsh"
        self.engine._run_proactive({
            "text": PROACTIVE_PROMPT, "request_id": "neko-proactive-x",
            "agent": "neko", "proactive": True})
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self._speak_commands(), [])

    def test_failure_is_logged_not_spoken(self):
        self.engine.selected = "neko"
        self.backend.answer = RuntimeError("N.E.K.O. offline")
        self.engine._run_proactive({
            "text": PROACTIVE_PROMPT, "request_id": "neko-proactive-err",
            "agent": "neko", "proactive": True})
        # No "遇到了问题" broadcast for an unsolicited turn.
        spoken = [c["text"] for c in self._speak_commands()]
        self.assertFalse(any("遇到了问题" in t for t in spoken))

    def test_normal_answer_also_uses_deliver_speech(self):
        self.engine.selected = "dsh"
        self.engine._run_job({"text": "你好", "request_id": "r-normal",
                              "agent": "dsh", "key": "你好"})
        speaks = self._speak_commands()
        self.assertEqual(len(speaks), 1)
        # The fake backend is shared by every agent in this test file.
        self.assertIn("忽然想到你", speaks[0]["text"])
        self.assertEqual(speaks[0]["agent"], "dsh")


    def test_music_playing_blocks_unsolicited_turns(self):
        """Two voices at once is never what the user asked for."""
        now = time.time()
        s2 = dict(self.engine.neko_proactive_settings(),
                  quiet_start="23:00", quiet_end="08:00")
        s2["quiet_start_min"], s2["quiet_end_min"] = 1380, 480
        noon = now - (time.localtime(now).tm_hour * 3600
                      + time.localtime(now).tm_min * 60) + 12 * 3600
        self.engine.selected = "neko"
        self.engine._proactive_last_at = noon - 3600
        self.engine.last_utterance = ("", noon - 3600)
        self.assertTrue(self.engine._proactive_gate(noon, s2))
        self.engine.audio_state = "playing"
        self.assertFalse(self.engine._proactive_gate(noon, s2))
        self.engine.audio_state = "idle"
        self.assertTrue(self.engine._proactive_gate(noon, s2))


if __name__ == "__main__":
    unittest.main()
