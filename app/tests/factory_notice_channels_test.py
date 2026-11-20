"""The factory's expiry notice is dropped on every input channel.

The clip used to be filtered only when it came from ASR. Typed console
lines and injected events still reached an assistant and left a grey
line in the chat window. Both entry points must honour the switch.
"""
import json
import tempfile
import unittest
import uuid
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
from engine import Engine  # noqa: E402

NOTICE = "由于你免费的内容服务已经到期了，请在若琪APP我的内容服务中续费"
VARIANTS = (NOTICE, "请在若琪app我的内容服务中续费", "内容服务已经到期")
QUESTION = "今天天气怎么样"


class _Backend:
    def __init__(self):
        self.calls = []

    def health(self):
        return True, ""

    def run(self, text, request_id, on_progress=None):
        self.calls.append(text)
        return "ok"

    def cancel(self, request_id):
        return True

    def close(self):
        pass


class FactoryNoticeChannelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = config_module.create_config(root / "config.json")
        self.config["_root"] = self.tmp.name
        self.config["_path"] = str(root / "config.json")
        # Never let a unit test reach the developer's real Roon sidecar.
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.engine = Engine(self.config, state_file=str(root / "state.json"))
        self.backend = _Backend()
        self.engine.backend_for = lambda agent_id: self.backend

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def event(self, text, kind="asr_final", source="rokid"):
        return self.engine.handle_event({
            "type": kind, "text": text, "source": source,
            "request_id": "t-" + uuid.uuid4().hex})

    def transcript_text(self):
        return "\n".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in Path(self.tmp.name).rglob("*.jsonl"))

    def test_voice_path_still_drops_the_notice(self):
        result = self.event(NOTICE)
        self.assertEqual(result["intent"], "factory_notice")

    def test_a_typed_event_drops_the_notice(self):
        result = self.event(NOTICE, kind="text", source="console")
        self.assertEqual(result["intent"], "factory_notice")

    def test_a_console_sourced_asr_event_drops_the_notice(self):
        result = self.event(NOTICE, kind="asr_final", source="console")
        self.assertEqual(result["intent"], "factory_notice")

    def test_chat_drops_the_notice_without_leaving_a_line(self):
        result = self.engine.chat(NOTICE, "dsh")
        self.assertEqual(result["intent"], "factory_notice")
        self.assertTrue(result["dropped"])
        self.assertEqual(self.engine.jobs.qsize(), 0)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(
            [c for c in self.engine.pending(0) if c["type"] == "speak"], [])
        self.assertNotIn(NOTICE, self.transcript_text())

    def test_every_variant_is_dropped_on_both_entry_points(self):
        for text in VARIANTS:
            self.assertEqual(self.event(text, "text", "console")["intent"],
                             "factory_notice", text)
            self.assertEqual(self.engine.chat(text, "dsh")["intent"],
                             "factory_notice", text)

    def test_a_real_question_still_reaches_the_assistant(self):
        voice = self.event(QUESTION)
        self.assertNotEqual(voice["intent"], "factory_notice")
        self.engine.jobs.join()
        typed = self.engine.chat(QUESTION, "dsh")
        self.assertNotEqual(typed["intent"], "factory_notice")
        self.assertFalse(typed.get("dropped"))
        self.engine.jobs.join()
        self.assertIn(QUESTION, self.backend.calls)

    def test_the_switch_still_controls_every_channel(self):
        self.engine.set_factory_notice(False)
        self.assertNotEqual(self.event(NOTICE, "text", "console")["intent"],
                            "factory_notice")
        self.assertNotEqual(self.engine.chat(NOTICE, "dsh")["intent"],
                            "factory_notice")
        self.engine.jobs.join()
        self.engine.set_factory_notice(True)
        self.assertEqual(self.event(NOTICE, "text", "console")["intent"],
                         "factory_notice")
        self.assertEqual(self.engine.chat(NOTICE, "dsh")["intent"],
                         "factory_notice")
