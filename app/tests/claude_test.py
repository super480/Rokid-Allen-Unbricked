"""The seventh assistant: 克劳德 (Claude Code CLI).

Covers the registry entry, the wake word and its sound-alike aliases,
the headless backend (argv, JSON result parsing, login error translation,
session resume and model switching), local auto-detection, and the voice
route to the new assistant.

Every CLI call is mocked at the process boundary: no test reaches the real
Claude service, and no test needs an account or a network connection
(00-START-HERE §5.8).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOST_DIR = Path(__file__).resolve().parents[1] / "host"
import sys  # noqa: E402
sys.path.insert(0, str(HOST_DIR))

import config as config_module  # noqa: E402
import intent  # noqa: E402
from backends import load_backend  # noqa: E402
from backends.claude import ClaudeBackend  # noqa: E402
from engine import Engine  # noqa: E402


def result_body(answer="好的，已完成", session_id="sess-1", is_error=False,
                subtype="success", **extra):
    payload = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": answer,
        "session_id": session_id,
        "duration_ms": 12,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


class FakeProcess:
    """Stand-in for subprocess.Popen: fixed stdout/stderr/returncode."""
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def communicate(self, timeout=None):
        return self.stdout, self.stderr

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


class FakeBackend:
    id = "claude"
    name = "克劳德"

    def __init__(self):
        self.answers = []

    def run(self, text, request_id, on_progress=None):
        self.answers.append(text)
        return "mock answer"


class RegistryTest(unittest.TestCase):
    def test_claude_is_registered(self):
        item = config_module.REGISTRY["claude"]
        self.assertEqual(item["name"], "克劳德")
        self.assertEqual(item["backend"], "claude")
        self.assertTrue(item["enabled"])
        self.assertIn("claude", config_module.BACKEND_SECTIONS)

    def test_backend_loader(self):
        backend = load_backend("claude", {}, {})
        self.assertIsInstance(backend, ClaudeBackend)

    def test_defaults_carry_a_headless_command(self):
        section = config_module.DEFAULTS["claude"]
        self.assertIn("-p", section["command"])
        self.assertIn("--output-format", section["command"])
        self.assertIn("json", section["command"])
        self.assertTrue(section["keep_session"])
        self.assertNotIn("token", section)     # no secret in shared config

    def test_wake_word_and_aliases(self):
        row = config_module.DEFAULTS["wake"]["agents"]["claude"]
        self.assertEqual(row["pinyin"], "ke4lao2de3")
        self.assertIn("克劳得", row["aliases"])
        self.assertIn("cloud", row["aliases"])
        # Common single characters must never be aliases.
        for alias in row["aliases"]:
            self.assertGreater(len(alias), 1, alias)

    def test_phonetic_table_has_the_word(self):
        self.assertIn("克劳德", intent.WAKE_PHONETIC["ke4lao2de3"])

    def test_exit_word(self):
        self.assertEqual(intent.parse_intent("退出克劳德")[0], "exit")


class WakeWordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config_module.create_config(
            Path(self.tmp.name) / "config.json")
        self.config["_root"] = self.tmp.name
        self.engine = Engine(self.config,
                             state_file=str(Path(self.tmp.name) / "state.json"))

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def test_vt_word_starts_with_display_name(self):
        self.engine.set_wake(enabled=True)
        row = self.engine.wake_settings()["agents"]["claude"]
        self.assertEqual(row["pinyin"], "ke4lao2de3")
        self.assertTrue(row["vt_word"].startswith("克劳德|"))
        self.assertNotIn("克劳德|克劳德", row["vt_word"])

    def test_label_and_prefix_route_to_claude(self):
        self.engine.set_wake(enabled=True)
        spellings = self.engine.wake_spellings()
        rows = self.engine.wake_settings()["agents"]
        pinyins = {key: row["pinyin"] for key, row in rows.items()}
        self.assertEqual(intent.wake_word_agent(
            "克劳德|克劳得|cloud", spellings, pinyins), "claude")
        self.assertEqual(intent.wake_word_agent(
            "ke4lao2de3", spellings, pinyins), "claude")
        self.assertEqual(intent.wake_word_agent(
            "柯劳德", spellings, pinyins), "claude")

    def test_select_by_name(self):
        for said in ("找克劳德", "切换到克劳德", "叫克劳得", "找柯劳德",
                     "找claude", "找cloud"):
            with self.subTest(said=said):
                self.assertEqual(intent.parse_intent(said),
                                 ("select", "claude"), said)

    def test_status_lists_the_new_face(self):
        status = self.engine.status()
        self.assertIn("claude", status["agents"])
        self.assertEqual(status["agents"]["claude"]["name"], "克劳德")

    def test_direct_mode_can_reuse_claude(self):
        backend = self.engine.backend_for("claude")
        self.assertIsInstance(backend, ClaudeBackend)


class BackendTest(unittest.TestCase):
    def make(self, settings=None):
        return ClaudeBackend(settings or dict(config_module.DEFAULTS["claude"]),
                             {})

    def test_health_reports_the_installed_cli(self):
        ok, detail = self.make().health()
        self.assertTrue(ok)
        self.assertIn("claude", detail)

    def test_build_argv_new_turn(self):
        backend = self.make()
        argv = backend.build_argv("你好", "")
        self.assertIn("-p", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertIn("--permission-prompts", argv)
        self.assertEqual(argv[-1], "你好")
        self.assertNotIn("--resume", argv)

    def test_build_argv_resumes_and_keeps_model_on_fresh_turn(self):
        backend = self.make({"command": ["claude", "-p", "--output-format", "json"],
                             "model": "sonnet"})
        resumed = backend.build_argv("再说一次", "sess-9")
        self.assertIn("--resume", resumed)
        self.assertIn("sess-9", resumed)
        self.assertNotIn("--model", resumed)
        fresh = backend.build_argv("你好", "")
        self.assertIn("--model", fresh)
        self.assertIn("sonnet", fresh)

    def test_parse_result_success(self):
        answer, session, error = ClaudeBackend.parse_result(
            result_body("答案在这里", "sess-2"))
        self.assertEqual(answer, "答案在这里")
        self.assertEqual(session, "sess-2")
        self.assertEqual(error, "")

    def test_parse_result_scans_json_lines(self):
        raw = "warning: something\n" + result_body("OK", "sess-3") + "\n"
        answer, session, error = ClaudeBackend.parse_result(raw)
        self.assertEqual(answer, "OK")
        self.assertEqual(session, "sess-3")
        self.assertEqual(error, "")

    def test_parse_result_login_error_is_flagged(self):
        answer, _session, error = ClaudeBackend.parse_result(
            result_body("Not logged in · Please run /login", "sess-4",
                        is_error=True, terminal_reason="api_error"))
        self.assertIn("Not logged in", error)

    def test_friendly_error_translates_login(self):
        text = ClaudeBackend.friendly_error("Not logged in · Please run /login")
        self.assertIn("还没登录", text)
        self.assertIn("claude", text)
        self.assertNotIn("Not logged in", text)

    def test_parse_result_empty_is_an_error(self):
        _answer, _session, error = ClaudeBackend.parse_result("not json at all")
        self.assertTrue(error)

    def test_models_and_switch(self):
        backend = self.make()
        names = backend.models()
        self.assertIn("sonnet", names)
        self.assertIn("opus", names)
        backend.switch_model("haiku")
        self.assertEqual(backend.current_model(), "haiku")
        self.assertIn("haiku", backend.models())

    def test_switch_model_drops_the_session(self):
        store = {}
        backend = ClaudeBackend(dict(config_module.DEFAULTS["claude"]), store)
        backend._remember_session("old-session")
        backend.switch_model("opus")
        self.assertEqual(backend._stored_session(), "")

    def test_run_returns_answer_and_remembers_session(self):
        store = {}
        backend = ClaudeBackend(dict(config_module.DEFAULTS["claude"]), store)
        process = FakeProcess(stdout=result_body("好的", "sess-5"))
        with mock.patch("subprocess.Popen", return_value=process) as popen:
            answer = backend.run("帮我写点东西", "r1")
        self.assertEqual(answer, "好的")
        self.assertEqual(store.get("claude_session"), "sess-5")
        argv = popen.call_args[0][0]
        self.assertEqual(argv[-1], "帮我写点东西")

    def test_run_uses_resume_on_next_turn(self):
        backend = self.make()
        backend._remember_session("sess-6")
        process = FakeProcess(stdout=result_body("继续", "sess-6"))
        with mock.patch("subprocess.Popen", return_value=process) as popen:
            backend.run("然后呢", "r2")
        argv = popen.call_args[0][0]
        self.assertIn("--resume", argv)
        self.assertIn("sess-6", argv)

    def test_login_failure_is_speakable(self):
        backend = self.make()
        raw = result_body("Not logged in · Please run /login", "sess-7",
                          is_error=True, terminal_reason="api_error")
        process = FakeProcess(stdout=raw, returncode=1)
        with mock.patch("subprocess.Popen", return_value=process):
            with self.assertRaises(Exception) as ctx:
                backend.run("你好", "r3")
        self.assertIn("还没登录", str(ctx.exception))

    def test_dead_session_retries_fresh(self):
        backend = self.make()
        backend._remember_session("gone")
        calls = []

        def popen_side_effect(argv, **kwargs):
            calls.append(list(argv))
            if "--resume" in argv:
                return FakeProcess(stdout=result_body(
                    "No conversation found with id gone", "", is_error=True))
            return FakeProcess(stdout=result_body("重新开始", "fresh"))

        with mock.patch("subprocess.Popen", side_effect=popen_side_effect):
            answer = backend.run("继续", "r4")
        self.assertEqual(answer, "重新开始")
        self.assertEqual(len(calls), 2)
        self.assertIn("--resume", calls[0])
        self.assertNotIn("--resume", calls[1])

    def test_empty_text_rejected(self):
        backend = self.make()
        with self.assertRaises(Exception):
            backend.run("  ", "r5")

    def test_cancel_stops_running_process(self):
        backend = self.make()
        process = FakeProcess(stdout=result_body("ok"))
        process.returncode = None
        with backend._lock:
            backend._process = process
        self.assertTrue(backend.cancel("r6"))
        self.assertTrue(process.terminated)


class DetectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config_module.create_config(
            Path(self.tmp.name) / "config.json")
        self.config["_root"] = self.tmp.name
        self.engine = Engine(self.config,
                             state_file=str(Path(self.tmp.name) / "state.json"))

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def test_claude_is_in_detection_with_suggested_word(self):
        detected = self.engine.detect_local_agents()
        self.assertIn("claude", detected)
        self.assertEqual(detected["claude"]["name"], "克劳德")
        self.assertEqual(detected["claude"]["suggested"], "ke4lao2de3")

    def test_login_state_is_reported_in_detail(self):
        probe = mock.Mock()
        probe.stdout = json.dumps({"loggedIn": False, "authMethod": "none"})
        with mock.patch("shutil.which", return_value="/tmp/example-cli/claude"), \
             mock.patch("subprocess.run", return_value=probe):
            detected = self.engine.detect_local_agents()
        self.assertTrue(detected["claude"]["available"])
        self.assertIn("未登录", detected["claude"]["detail"])
        self.assertIn("claude /login", detected["claude"]["detail"])

    def test_slow_probe_does_not_break_detection(self):
        with mock.patch("shutil.which", return_value="/tmp/example-cli/claude"), \
             mock.patch("subprocess.run", side_effect=OSError("slow")):
            detected = self.engine.detect_local_agents()
        self.assertTrue(detected["claude"]["available"])
        self.assertIn("已安装", detected["claude"]["detail"])


class RoutingTest(unittest.TestCase):
    """Selecting 克劳德 routes ordinary turns to its own backend."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config_module.create_config(
            Path(self.tmp.name) / "config.json")
        self.config["_root"] = self.tmp.name
        self.config["roon_bridge_url"] = "http://127.0.0.1:1"
        self.engine = Engine(self.config,
                             state_file=str(Path(self.tmp.name) / "state.json"))
        # The home gate is a local regex; enabling it opens no socket.
        # The forwarding path itself is mocked (00-START-HERE §5.8).
        self.engine.homeassistant.config.update({
            "enabled": True, "base_url": "http://ha.test:8123",
            "token": "test-token"})
        self.bridge_hits = []
        self.engine.homeassistant.handle_text = lambda text, request_id="": (
            self.bridge_hits.append(text) or None)
        self.backend = FakeBackend()
        self.engine.backend_for = lambda agent_id: self.backend
        self.counter = 0

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def say(self, text):
        self.counter += 1
        result = self.engine.handle_event({
            "type": "asr_final", "text": text,
            "request_id": "r%d" % self.counter})
        self.engine.jobs.join()
        return result

    def test_select_and_talk(self):
        self.say("找克劳德")
        self.assertEqual(self.engine.selected, "claude")
        result = self.say("帮我解释一下这段代码")
        self.assertEqual(result["intent"], "command")
        self.assertEqual(self.backend.answers, ["帮我解释一下这段代码"])

    def test_exit_returns_to_factory(self):
        self.say("找克劳德")
        result = self.say("退出克劳德")
        self.assertEqual(result["intent"], "exit")
        self.assertIsNone(self.engine.selected)

    def test_home_turn_is_redirected_like_other_agents(self):
        # 家居语音归属是最终规则：只有若琪和小家能控制设备，克劳德
        # 与其它助手一样只回固定指引，不执行、不转发、不进模型。
        self.say("找克劳德")
        result = self.say("打开茶几灯")
        self.assertEqual(result["intent"], "homeassistant")
        self.assertEqual(result["status"], "redirected")
        self.assertEqual(result["speech"], "控制家居设备请找小家")
        # "找克劳德" itself may open a clarification-window probe while
        # 若琪 is still selected; the home sentence must never be forwarded.
        self.assertNotIn("打开茶几灯", self.bridge_hits)
        self.assertEqual(self.backend.answers, [])


if __name__ == "__main__":
    unittest.main()
