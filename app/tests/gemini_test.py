"""Gemini transport and routing with a simulated Antigravity CLI."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config  # noqa: E402
import intent  # noqa: E402
from backends.base import BackendError  # noqa: E402
from backends.gemini import DEFAULT_MODEL, GeminiBackend, LOGIN_HINT  # noqa: E402
from engine import Engine  # noqa: E402


class GeminiTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = temporary.name
        self.path = Path(directory) / "config.json"
        config.create_config(self.path)
        settings = config.load_config(self.path)
        settings["roon_bridge_url"] = "http://127.0.0.1:1"
        self.engine = Engine(settings, state_file=str(Path(directory) / "state.json"))
        self.addCleanup(self.engine.close)
        finder = mock.patch("backends.gemini.executable", return_value="/fake/agy")
        finder.start()
        self.addCleanup(finder.stop)
        patcher = mock.patch("backends.gemini.subprocess.run", side_effect=self.run_cli)
        self.addCleanup(patcher.stop)
        self.send = patcher.start()
    @staticmethod
    def run_cli(args, **kwargs):
        if args[-1] == "models":
            return subprocess.CompletedProcess(args, 0, DEFAULT_MODEL + "    Gemini\n", "")
        event = {"event": "result", "result": {"status": "SUCCESS", "response": "收到。"}}
        return subprocess.CompletedProcess(args, 0, json.dumps(event), "")

    def test_voice_chat_and_direct_have_the_expected_context_and_avatar(self):
        selected = self.engine.handle_event({
            "type": "asr_final", "text": "找哈吉米", "request_id": "select",
        })
        self.assertEqual(selected["selected_agent"], "gemini")
        agent = self.engine.status()["agents"]["gemini"]
        self.assertEqual(agent["avatar"], "gemini")
        self.assertEqual(agent["name"], "哈基米")

        self.engine.handle_event({
            "type": "asr_final", "text": "介绍一下自己", "request_id": "voice",
        })
        self.engine.jobs.join()
        self.engine.chat("再说一句", "gemini")
        self.engine.jobs.join()
        self.engine.set_direct_settings({"source": "assistant", "assistant": "gemini"})
        self.engine.chat("直连提问", "direct")
        self.engine.jobs.join()

        voice, followup, direct = [call for call in self.send.call_args_list
                                  if "input" in call.kwargs]
        self.assertIn(DEFAULT_MODEL, voice.args[0])
        options = json.loads(voice.args[0][2])
        self.assertFalse(options["useG1Credits"])
        self.assertEqual(options["modelProvider"], "")
        self.assertIn("command(*)", options["permissions"]["deny"])
        prompts = [json.loads(call.kwargs["input"])["message"]["content"]
                   for call in (voice, followup, direct)]
        self.assertEqual(prompts[0], "介绍一下自己")
        self.assertTrue(prompts[1].endswith("用户：介绍一下自己\n哈基米：收到。\n用户：再说一句"))
        self.assertEqual(prompts[2], "直连提问")
        spoken = [(command["agent"], command["text"])
                  for command in self.engine.pending(0)
                  if command["type"] == "speak" and command.get("request_id")]
        self.assertEqual(spoken, [
            ("gemini", "收到。"), ("gemini", "收到。"), ("direct", "收到。"),
        ])

    def test_hajimi_and_asr_aliases_route_to_gemini(self):
        self.engine.set_wake(enabled=True)
        wake = self.engine.wake_settings()["agents"]["gemini"]
        self.assertEqual(wake["pinyin"], "ha1ji2mi3")
        labels = wake["vt_word"].split("|")
        self.assertEqual(labels, ["哈基米", "哈吉米", "哈极米", "哈集米",
                                  "哈基咪", "哈吉咪", "哈鸡米", "哈奇米"])
        for label in labels:
            with self.subTest(label=label):
                self.assertEqual(intent.parse_intent("若琪，找" + label), ("select", "gemini"))
                self.assertEqual(intent.wake_prefix(label + "，你好", self.engine.wake_spellings()),
                                 ("gemini", "你好"))
        selected = self.engine.handle_event({"type": "wake_word", "word": "哈基咪"})
        self.assertEqual(selected["selected_agent"], "gemini")
        self.assertEqual(intent.parse_intent("退出哈基米"), ("exit", None))

    def test_settings_are_persisted_and_used_by_both_backends(self):
        self.engine.config["gemini"]["api_key"] = "legacy-key"
        self.engine.set_direct_settings({"source": "assistant", "assistant": "gemini"})
        old_direct = self.engine.backend_for("direct").inner()

        self.engine.set_agent_model("direct", "gemini-custom")
        saved = config.load_config(self.path)["gemini"]
        self.assertEqual(saved["model"], "gemini-custom")
        self.assertNotIn("api_key", saved)
        self.assertEqual(self.engine.backend_for("gemini").current_model(), "gemini-custom")
        new_direct = self.engine.backend_for("direct").inner()
        self.assertIsNot(old_direct, new_direct)
        self.assertEqual(new_direct.current_model(), "gemini-custom")

        result = self.engine.test_gemini()
        self.assertTrue(result["ok"])
        self.assertEqual(result["models"], [DEFAULT_MODEL])
        self.assertEqual(self.send.call_args.args[0][-1], "models")
        self.assertEqual(new_direct.turns, [])

    def test_signed_out_cli_never_starts_a_model_request_or_uses_api_key(self):
        self.send.side_effect = None
        self.send.return_value = subprocess.CompletedProcess([], 1, "", "Please sign in")
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "old-key", "AGY_ADC_AUTH": "true"}):
            with self.assertRaisesRegex(BackendError, LOGIN_HINT):
                GeminiBackend({"api_key": "old-key"}).run("你好", "signed-out")
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0][-1], "models")
        self.assertNotIn("GEMINI_API_KEY", self.send.call_args.kwargs["env"])
        self.assertNotIn("AGY_ADC_AUTH", self.send.call_args.kwargs["env"])

    def test_quota_exhaustion_does_not_retry_with_credits(self):
        result = {"event": "result", "result": {"status": "ERROR", "error": "429 quota limit"}}
        self.send.side_effect = [self.run_cli(["models"]),
                                 subprocess.CompletedProcess([], 1, json.dumps(result), "")]
        with self.assertRaisesRegex(BackendError, "额度暂不可用"):
            GeminiBackend({}).run("你好", "quota")
        self.assertEqual(self.send.call_count, 2)
        self.assertFalse(json.loads(self.send.call_args.args[0][2])["useG1Credits"])
