"""The engine: routing a spoken turn to the right assistant."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
import engine as engine_module  # noqa: E402
from backends.direct import HYBRID, OFF  # noqa: E402
from engine import Engine  # noqa: E402


class FakeBackend:
    """A backend that answers with a fixed line and records the progress."""

    def __init__(self, settings=None, state_store=None):
        self.answers = []
        self.progress = []
        self.state_store = state_store

    def run(self, text, request_id, on_progress=None):
        self.answers.append(text)
        if on_progress:
            on_progress("thinking", "")
            on_progress("working", "执行命令")
        return "收到：" + text

    def cancel(self, request_id):
        return True

    def close(self):
        pass

    def health(self):
        return True, "ok"


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.config = config_module.create_config(Path(self.tmp.name) / "config.json")
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.engine = Engine(self.config, state_file=str(self.state))
        self.backend = FakeBackend()
        self.engine.backend_for = lambda agent_id: self.backend
        # This file covers the routing to an assistant. Direct mode has its
        # own tests, so the engine starts with direct mode switched off.
        self.engine.set_direct_mode(OFF)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def test_selecting_hermes_switches_the_session(self):
        result = self.engine.handle_event({
            "type": "asr_final", "text": "找小赫", "request_id": "r1"})
        self.assertEqual(result["intent"], "select")
        self.assertEqual(self.engine.selected, "hermes")

    def test_a_task_goes_to_the_selected_assistant(self):
        self.engine.handle_event({
            "type": "asr_final", "text": "找小赫", "request_id": "r1"})
        result = self.engine.handle_event({
            "type": "asr_final", "text": "列出目录", "request_id": "r2"})
        self.assertEqual(result["intent"], "command")
        self.engine.jobs.join()
        self.assertEqual(self.backend.answers, ["列出目录"])

    def test_a_task_without_an_assistant_is_refused(self):
        result = self.engine.handle_event({
            "type": "asr_final", "text": "列目录", "request_id": "r1"})
        # An empty string, not null: the device reads this field with
        # JSONObject.optString, which turns null into the text "null".
        self.assertEqual(result["selected_agent"], "")
        self.assertEqual(self.backend.answers, [])

    def test_a_question_sent_while_busy_is_queued_not_dropped(self):
        """A question asked while the assistant is busy must still be answered.

        The slow assistants -- DeepSeek runs commands and can take a
        minute -- used to throw away every question that arrived during
        that time. Nothing was written down either, so the console showed
        nothing at all and the input looked as if it had never arrived.
        """
        self.engine.handle_event({
            "type": "asr_final", "text": "找小赫", "request_id": "r1"})

        # Pretend a turn is running.
        self.engine.busy = True
        result = self.engine.chat("第二个问题", "hermes")
        self.assertTrue(result["accepted"])
        self.assertFalse(result.get("dropped"))
        # It is written down straight away, so the console shows it.
        messages = [item["message"] for item in self.engine.transcript]
        self.assertIn("第二个问题", messages)

    def test_a_question_sent_while_busy_is_answered_in_turn(self):
        """The queued question is answered, not just recorded."""
        self.engine.handle_event({
            "type": "asr_final", "text": "找小赫", "request_id": "r1"})
        self.engine.busy = True
        self.engine.chat("第二个问题", "hermes")
        self.engine.busy = False
        self.engine.jobs.join()
        self.assertEqual(self.backend.answers, ["第二个问题"])

    def test_the_same_words_twice_are_one_question(self):
        """The recogniser can report one utterance twice; that is not two."""
        self.engine.handle_event({
            "type": "asr_final", "text": "找小赫", "request_id": "r1"})
        # Both deliveries arrive while the original question is pending.
        with self.engine.lock:
            first = self.engine.chat("同一个问题", "hermes")
            second = self.engine.chat("同一个问题", "hermes")
        self.assertFalse(first.get("duplicate"))
        self.assertTrue(second.get("duplicate"))
        self.engine.jobs.join()
        self.assertEqual(self.backend.answers, ["同一个问题"])

    def test_a_full_queue_says_so_instead_of_going_quiet(self):
        """Even a refused question is written down, so it is not a mystery."""
        self.engine.handle_event({
            "type": "asr_final", "text": "找小赫", "request_id": "r1"})
        # Fill the queue.
        for index in range(engine_module.MAX_PENDING_TURNS + 2):
            self.engine.chat("问题%d" % index, "hermes")
        messages = [item["message"] for item in self.engine.transcript]
        self.assertTrue(any("队列已满" in text for text in messages))

    def test_status_reports_no_selection_as_an_empty_string(self):
        self.assertEqual(self.engine.status()["selected_agent"], "")

    def test_duplicate_request_id_is_ignored(self):
        event = {"type": "asr_final", "text": "找小赫", "request_id": "r1"}
        self.engine.handle_event(event)
        second = self.engine.handle_event(event)
        self.assertTrue(second.get("duplicate"))

    def test_wake_event_switches_to_listening(self):
        self.engine.handle_event({"type": "wake"})
        commands = self.engine.pending(0)
        self.assertTrue(any(c["type"] == "state" and c["state"] == "listening"
                            for c in commands))

    def test_exit_returns_to_the_factory(self):
        self.engine.handle_event({
            "type": "asr_final", "text": "找小赫", "request_id": "r1"})
        self.engine.handle_event({
            "type": "asr_final", "text": "退出助手", "request_id": "r2"})
        self.assertIsNone(self.engine.selected)

    def test_a_factory_turn_still_reaches_the_transcript(self):
        # The factory answers "几点了" itself. Its words never reach us,
        # but the question and who answered it still belong in the
        # transcript, otherwise the console looks like nothing happened.
        self.engine.set_direct_mode(engine_module.HYBRID)
        self.engine.handle_event({
            "type": "asr_final", "text": "今天星期几", "request_id": "r1"})
        rows = self.engine.transcript_since(0)["items"]
        asked = [r for r in rows if r["role"] == "user"]
        answered = [r for r in rows if r["role"] == "agent"]
        self.assertTrue(any("今天星期几" in r["message"] for r in asked))
        self.assertTrue(any("原厂" in r["message"] for r in answered))
        for row in asked + answered:
            self.assertEqual(row["agent"], "direct")

    def test_leaving_without_an_assistant_stays_quiet(self):
        # Nothing was selected, so there is nothing to leave: no line in
        # the transcript and no announcement on the speaker.
        before = len(self.engine.transcript_since(0)["items"])
        self.engine.handle_event({
            "type": "asr_final", "text": "退出助手", "request_id": "r1"})
        after = self.engine.transcript_since(0)["items"]
        self.assertEqual(len(after), before)
        self.assertFalse([r for r in after if "已退出助手" in r["message"]])
        spoken = [c for c in self.engine.pending(0)
                  if c["type"] == "speak" and "已退出助手" in c.get("text", "")]
        self.assertFalse(spoken)

    def test_empty_text_is_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.handle_event({
                "type": "asr_final", "text": "  ", "request_id": "r1"})

    def test_missing_request_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.handle_event({"type": "asr_final", "text": "你好"})

    def test_unsupported_event_type_is_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.handle_event({"type": "nonsense", "request_id": "r1"})

    def test_progress_is_forwarded_to_the_screen(self):
        self.engine.handle_event({
            "type": "asr_final", "text": "找小赫", "request_id": "r1"})
        self.engine.handle_event({
            "type": "asr_final", "text": "干活", "request_id": "r2"})
        self.engine.jobs.join()
        states = [c.get("state") for c in self.engine.pending(0)]
        self.assertIn("working", states)

    def test_status_lists_the_assistants(self):
        status = self.engine.status()
        self.assertIn("hermes", status["agents"])
        self.assertTrue(status["agents"]["hermes"]["enabled"])

    # -- echo and duplicates -------------------------------------------------

    def test_the_same_wording_twice_is_one_utterance(self):
        # Both ASR channels report the same sentence; the second one
        # carries a different request id, so the id guard cannot collapse
        # them.
        first = self.engine.handle_event({
            "type": "asr_final", "text": "列目录", "request_id": "r1"})
        second = self.engine.handle_event({
            "type": "asr_final", "text": "列目录", "request_id": "r2"})
        self.assertEqual(first["intent"], "command")
        self.assertTrue(second.get("duplicate"))

    def test_the_echo_of_our_own_broadcast_is_dropped(self):
        # The speaker hears "先说找小赫，我才能接你的任务。" and the
        # recogniser hands it back as "找小鹤". Answering that would
        # repeat the broadcast for ever.
        self.engine.handle_event({
            "type": "asr_final", "text": "列目录", "request_id": "r1"})
        result = self.engine.handle_event({
            "type": "asr_final", "text": "找小鹤", "request_id": "r2"})
        self.assertEqual(result["intent"], "echo")
        self.assertTrue(result.get("duplicate"))

    def test_a_genuine_sentence_after_a_broadcast_is_answered(self):
        self.engine.handle_event({
            "type": "asr_final", "text": "找小赫", "request_id": "r1"})
        result = self.engine.handle_event({
            "type": "asr_final", "text": "现在几点了", "request_id": "r2"})
        self.assertEqual(result["intent"], "command")
        self.engine.jobs.join()
        self.assertEqual(self.backend.answers, ["现在几点了"])

    def test_typed_console_input_is_never_treated_as_an_echo(self):
        self.engine.handle_event({
            "type": "asr_final", "text": "找小赫", "request_id": "r1"})
        result = self.engine.handle_event({
            "type": "text", "text": "找小赫", "request_id": "r2",
            "source": "console"})
        self.assertNotEqual(result.get("intent"), "echo")

    def test_the_echo_filter_does_not_swallow_real_questions(self):
        # A normal question after a broadcast must still be answered.
        pairs = [
            ("2026-11-02 20:35:39 CST（周一）。", "现在几点了", False),
            ("2026年11月2日（周一），CST 时区。", "今天几号", False),
            ("Hermes在，请说你的任务。", "现在几点了", False),
            ("已退出助手。", "退出助手", True),
            ("先说找小赫，我才能接你的任务。", "找小鹤", True),
            ("先说找小赫，我才能接你的任务。", "找小贺", True),
            ("已退出助手。", "今天天气怎么样", False),
            ("我记住了，数字是77。", "记住数字78", False),
        ]
        for spoken, heard, expect_echo in pairs:
            self.engine.recent_speeches.clear()
            self.engine.emit("speak", text=spoken)
            self.assertEqual(
                self.engine.is_echo(heard), expect_echo,
                "%r heard after %r" % (heard, spoken))

    def test_saying_the_name_after_the_hint_is_not_an_echo(self):
        # The hint "先说找小赫，我才能接你的任务。" contains the name. The
        # user copies it straight away -- that must select the assistant,
        # not be swallowed as the echo of the hint.
        self.engine.handle_event({
            "type": "asr_final", "text": "列目录", "request_id": "r1"})
        self.engine.handle_event({
            "type": "asr_final", "text": "找小鹤", "request_id": "r2"})
        # The device reports the broadcast finished.
        self.engine.handle_event({"type": "playback_done"})
        result = self.engine.handle_event({
            "type": "asr_final", "text": "找小鹤", "request_id": "r3"})
        self.assertEqual(result["intent"], "select")

    def test_the_echo_during_the_broadcast_is_dropped(self):
        # While the device is still talking, anything it hears is our own
        # voice.
        self.engine.handle_event({
            "type": "asr_final", "text": "列目录", "request_id": "r1"})
        result = self.engine.handle_event({
            "type": "asr_final", "text": "找小鹤", "request_id": "r2"})
        self.assertEqual(result["intent"], "echo")

    # -- TTS recovery --------------------------------------------------------

    def test_a_stuck_engine_triggers_a_restart(self):
        calls = []

        class FakeRecovery:
            def available(self):
                return True

            def restart(self):
                calls.append("restart")
                return True

        self.engine.tts_recovery = FakeRecovery()
        result = self.engine.handle_event({
            "type": "tts_stuck", "error": 2, "failures": 2})
        self.assertEqual(result["intent"], "recover")
        # The restart runs on its own thread; give it a moment.
        for _ in range(50):
            if calls:
                break
            time.sleep(0.02)
        self.assertEqual(calls, ["restart"])

    def test_a_stuck_engine_without_a_device_is_reported(self):
        class FakeRecovery:
            def available(self):
                return False

            def restart(self):
                raise AssertionError("must not restart")

        self.engine.tts_recovery = FakeRecovery()
        result = self.engine.handle_event({
            "type": "tts_stuck", "error": 2, "failures": 2})
        self.assertEqual(result["intent"], "recover")

    def test_a_silence_is_not_answered(self):
        # The recogniser prints the word "null" when it heard nothing
        # usable. Answering that would make the device talk at a silence.
        for index, said in enumerate(("null", "若琪")):
            result = self.engine.handle_event({
                "type": "asr_final", "text": said,
                "request_id": "n1-%d" % index})
            self.assertEqual(result["intent"], "empty", "%r" % said)
        self.assertEqual(self.backend.answers, [])

    def test_an_empty_text_is_refused_by_the_input_check(self):
        for said in ("", "   "):
            with self.assertRaises(ValueError):
                self.engine.handle_event({
                    "type": "asr_final", "text": said, "request_id": "n2"})

    def test_a_misheard_name_selects_the_assistant(self):
        result = self.engine.handle_event({
            "type": "asr_final", "text": "找小鹤", "request_id": "r1"})
        self.assertEqual(result["intent"], "select")
        self.assertEqual(self.engine.selected, "hermes")

class ModelChangeTest(unittest.TestCase):
    """A model change has to reach every conversation that answers with it.

    Direct mode reuses an assistant but keeps a conversation of its own,
    with its own backend and its own session. A change made on the
    assistant does not reach it on its own, and a conversation that keeps
    its old session answers with the old model.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.config = config_module.create_config(Path(self.tmp.name) / "config.json")
        self.config["_root"] = self.tmp.name
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.config["direct"]["mode"] = HYBRID
        self.config["direct"]["source"] = "assistant"
        self.config["direct"]["assistant"] = "hermes"
        self.config.setdefault("agents", {})["hermes"] = {"model": "model-old"}
        self.engine = Engine(self.config, state_file=str(self.state))
        self.engine._store["direct_session"] = "local-old"
        self.engine._store["direct_session_model"] = "model-old"

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def test_changing_the_assistants_model_reaches_direct_mode(self):
        direct = self.engine.backend_for("direct")
        inner = direct.inner()
        self.assertEqual(inner.model, "model-old")
        self.engine.set_agent_model("hermes", "model-new")
        self.assertEqual(inner.model, "model-new")

    def test_changing_the_direct_model_reaches_the_assistant(self):
        self.engine.set_agent_model("direct", "model-new")
        self.assertEqual(self.config["agents"]["hermes"]["model"], "model-new")

    def test_a_model_change_drops_the_direct_session(self):
        direct = self.engine.backend_for("direct")
        inner = direct.inner()
        self.engine.set_agent_model("hermes", "model-new")
        # The old session runs on the old model and can not be kept.
        self.assertNotIn("direct_session", self.engine.store)
        self.assertEqual(inner.session_for_model(), "")
        # The next turn starts a fresh session that carries the model.
        self.assertIn("--model", inner.build_argv("你好", inner.session_for_model()))

    def test_the_model_a_turn_ran_on_is_reported(self):
        """A fallback to another model must not pass unnoticed."""
        direct = self.engine.backend_for("direct")
        inner = direct.inner()
        self.assertEqual(self.engine.agent_in_use_model("direct"), "")
        inner.used_model = "model-actually-used"
        self.assertEqual(self.engine.agent_in_use_model("direct"), "model-actually-used")

    def test_a_direct_custom_endpoint_has_no_model_to_report(self):
        self.engine.config["direct"]["source"] = "custom"
        self.engine.config["direct"]["custom"] = {
            "base_url": "https://x.test/v1", "api_key": "k", "model": "m"}
        self.engine.backend_for("direct")
        self.assertEqual(self.engine.agent_in_use_model("direct"), "")

    def test_a_model_change_drops_the_stored_session_without_a_backend(self):
        """After a restart the direct backend does not exist yet.

        The session lives in the state file and outlives the backend, so
        the change has to reach it there as well.
        """
        self.assertNotIn("direct", self.engine.backends)
        self.assertIn("direct_session", self.engine.store)
        self.engine.set_agent_model("hermes", "model-new")
        self.assertNotIn("direct_session", self.engine.store)
        self.assertNotIn("direct_session_model", self.engine.store)

    def test_a_model_change_drops_the_assistants_own_session(self):
        """The assistant's session is in the state file as well."""
        self.engine._store["hermes_session"] = "local-old"
        self.engine._store["hermes_session_model"] = "model-old"
        self.engine._store["hermes_session_provider"] = "minimax-cn"
        self.engine.set_agent_model("hermes", "model-new")
        self.assertNotIn("hermes_session", self.engine.store)
        self.assertNotIn("hermes_session_model", self.engine.store)
        self.assertNotIn("hermes_session_provider", self.engine.store)


if __name__ == "__main__":
    unittest.main()
