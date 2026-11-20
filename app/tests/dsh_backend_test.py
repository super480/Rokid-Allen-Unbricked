"""The DSH backend: argument building, session handling and the event stream."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from backends.base import BackendError  # noqa: E402
from backends.dsh import DshBackend  # noqa: E402
import backends.dsh as dsh_module  # noqa: E402

# A stand-in for the dsh CLI: it prints the same event stream the real one
# prints, so the parsing is tested against the real shape of the output.
FAKE_CLI = '''#!/usr/bin/env python3
import os, sys
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "error":
    print('{"type":"error","message":"session \\"session-gone\\" does not exist; omit --session-id"}')
    sys.exit(1)
if mode == "empty":
    sys.exit(0)
print('{"type":"session","sessionId":"session-fake-1","cwd":"/tmp"}')
print('{"type":"status","phase":"turn_start","turn":1}')
print('{"type":"thinking","text":"let me look"}')
print('{"type":"tool_call","callId":"c1","tool":"bash","input":{"command":"ls"}}')
print('{"type":"tool_result","callId":"c1","status":"completed","result":"ok"}')
print('{"type":"text","text":"答案是42"}')
print('{"type":"final","text":"答案是42"}')
'''


def make_fake_cli(directory, mode="ok"):
    path = Path(directory) / "fake-dsh"
    path.write_text(FAKE_CLI, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["FAKE_MODE"] = mode
    return str(path)


class DshBackendTest(unittest.TestCase):
    def make(self, **settings):
        base = {
            "command": ["dsh", "headless", "--json"],
            "cwd": "",
            "timeout_seconds": 30,
            "keep_session": True,
            "permission_mode": "danger-full-access",
        }
        base.update(settings)
        return DshBackend(base, state_store={})

    # -- arguments ------------------------------------------------------------

    def test_argv_uses_the_headless_profile(self):
        backend = self.make()
        argv = backend.build_argv("你好", "")
        self.assertIn("headless", argv)
        self.assertIn("--json", argv)
        self.assertEqual(argv[-1], "你好")

    def test_argv_resumes_a_kept_session(self):
        backend = self.make()
        argv = backend.build_argv("继续", "session-abc")
        self.assertIn("--session-id", argv)
        self.assertIn("session-abc", argv)
        # The task has to stay behind the options.
        self.assertEqual(argv[-1], "继续")

    def test_argv_omits_the_session_without_one(self):
        backend = self.make()
        self.assertNotIn("--session-id", backend.build_argv("继续", ""))

    # -- session --------------------------------------------------------------

    def test_the_session_is_stored_for_the_next_turn(self):
        store = {}
        backend = self.make()
        backend.state_store = store
        backend._remember_session("session-xyz")
        self.assertEqual(store["dsh_session"], "session-xyz")

    def test_reset_forgets_the_session(self):
        store = {"dsh_session": "session-xyz"}
        backend = self.make()
        backend.state_store = store
        backend.reset_session()
        self.assertNotIn("dsh_session", store)

    # -- environment ----------------------------------------------------------

    def test_the_permission_mode_reaches_the_child(self):
        backend = self.make(permission_mode="read-only")
        self.assertEqual(backend.build_env()["DSH_PERMISSION_MODE"], "read-only")

    def test_the_default_permission_mode_needs_no_approval(self):
        # The headless profile has no approval channel, so the default has to
        # be the mode that asks for nothing.
        backend = self.make()
        self.assertEqual(backend.build_env()["DSH_PERMISSION_MODE"],
                         "danger-full-access")

    # -- the binary -----------------------------------------------------------

    def test_a_configured_binary_is_not_replaced_by_another_one(self):
        backend = self.make(command=["definitely-not-installed-xyz"])
        self.assertIsNone(backend.executable())

    def test_health_reports_the_missing_binary(self):
        backend = self.make(command=["definitely-not-installed-xyz"])
        ok, detail = backend.health()
        self.assertFalse(ok)
        self.assertIn("dsh", detail)

    def test_tool_labels_cover_the_common_tools(self):
        from backends.dsh import TOOL_LABELS
        self.assertEqual(TOOL_LABELS["bash"], "执行命令")
        self.assertEqual(TOOL_LABELS["web_search"], "联网搜索")

    # -- the event stream -----------------------------------------------------

    def test_the_answer_comes_from_the_final_event(self):
        with tempfile.TemporaryDirectory() as folder:
            cli = make_fake_cli(folder)
            backend = self.make(command=[cli])
            self.assertEqual(backend.run("几点了", "r1"), "答案是42")

    def test_the_session_id_is_remembered_from_the_session_event(self):
        with tempfile.TemporaryDirectory() as folder:
            cli = make_fake_cli(folder)
            store = {}
            backend = self.make(command=[cli])
            backend.state_store = store
            backend.run("你好", "r1")
            self.assertEqual(store.get("dsh_session"), "session-fake-1")

    def test_a_tool_call_is_reported_as_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            cli = make_fake_cli(folder)
            backend = self.make(command=[cli])
            seen = []
            backend.run("列目录", "r1", lambda state, detail: seen.append((state, detail)))
            states = [state for state, _ in seen]
            self.assertIn("thinking", states)
            self.assertIn("working", states)
            self.assertIn("speaking", states)
            working = [detail for state, detail in seen if state == "working"]
            self.assertEqual(working[0], "执行命令")

    def test_an_error_event_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            cli = make_fake_cli(folder, mode="error")
            backend = self.make(command=[cli])
            with self.assertRaises(BackendError):
                backend.run("你好", "r1")

    def test_no_answer_is_reported_as_a_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            cli = make_fake_cli(folder, mode="empty")
            backend = self.make(command=[cli])
            with self.assertRaises(BackendError):
                backend.run("你好", "r1")

    # -- recovery -------------------------------------------------------------

    def test_a_dead_session_restarts_the_conversation(self):
        """A session the CLI no longer knows about must not lose the turn."""
        calls = []

        class Fake(DshBackend):
            def _run_once(self, text, request_id, on_progress=None):
                calls.append(text)
                if len(calls) == 1:
                    raise BackendError(
                        'DeepSeek 出错：session "session-gone" does not exist')
                return "重新开始后的回答"

        backend = Fake({"command": ["dsh"], "timeout_seconds": 5},
                       {"dsh_session": "session-gone"})
        answer = backend.run("你好", "r1")
        self.assertEqual(answer, "重新开始后的回答")
        self.assertEqual(len(calls), 2)
        # The dead session must be dropped so the retry starts fresh.
        self.assertEqual(backend.state_store.get("dsh_session", ""), "")

    def test_other_failures_are_not_retried(self):
        calls = []

        class Fake(DshBackend):
            def _run_once(self, text, request_id, on_progress=None):
                calls.append(text)
                raise BackendError("DeepSeek 出错：模型不可用")

        backend = Fake({"command": ["dsh"], "timeout_seconds": 5}, {})
        with self.assertRaises(BackendError):
            backend.run("你好", "r1")
        self.assertEqual(len(calls), 1)

    def test_a_locked_session_is_dropped_and_the_turn_is_kept(self):
        """A session another process holds must not disable the assistant.

        The lock is left behind by a turn that was killed, or by a second
        host running against the same session. Resuming it again would
        fail identically, so the session has to be dropped and a new one
        started -- otherwise the assistant stays unusable for good.
        """
        calls = []

        class Fake(DshBackend):
            def _run_once(self, text, request_id, on_progress=None):
                calls.append(text)
                if len(calls) == 1:
                    raise BackendError(
                        'DeepSeek 出错：session "session-locked" is already owned '
                        "by an active write handle")
                return "换了个会话的回答"

        backend = Fake({"command": ["dsh"], "timeout_seconds": 5},
                       {"dsh_session": "session-locked"})
        answer = backend.run("你好", "r1")
        self.assertEqual(answer, "换了个会话的回答")
        self.assertEqual(len(calls), 2)
        # The locked session must be dropped, or the retry would hit it
        # again.
        self.assertEqual(backend.state_store.get("dsh_session", ""), "")

    def test_a_session_from_another_directory_is_dropped(self):
        """A session recorded under a different folder cannot be resumed."""
        calls = []

        class Fake(DshBackend):
            def _run_once(self, text, request_id, on_progress=None):
                calls.append(text)
                if len(calls) == 1:
                    raise BackendError(
                        'DeepSeek 出错：session "session-x" was recorded in '
                        '"/somewhere/else", not "/here"')
                return "新目录的回答"

        backend = Fake({"command": ["dsh"], "timeout_seconds": 5},
                       {"dsh_session": "session-x"})
        answer = backend.run("你好", "r1")
        self.assertEqual(answer, "新目录的回答")
        self.assertEqual(backend.state_store.get("dsh_session", ""), "")

    def test_a_cut_stream_is_tried_again(self):
        """The upstream stream can end before the model finished."""
        calls = []

        class Fake(DshBackend):
            def _run_once(self, text, request_id, on_progress=None):
                calls.append(text)
                if len(calls) == 1:
                    raise BackendError(
                        "DeepSeek 出错：dsh: STREAM_CLOSED: DeepSeek Messages "
                        "stream ended before message_stop")
                return "重试后的回答"

        backend = Fake({"command": ["dsh"], "timeout_seconds": 5}, {})
        answer = backend.run("你好", "r1")
        self.assertEqual(answer, "重试后的回答")
        self.assertEqual(len(calls), 2)

    def test_an_overloaded_service_is_tried_again_and_stays_bounded(self):
        """A busy upstream is worth one more try -- but not for ever.

        Without a limit, a turn that keeps being refused never ends, and
        the engine waits for an answer that will never come.
        """
        calls = []

        class Fake(DshBackend):
            def _run_once(self, text, request_id, on_progress=None):
                calls.append(text)
                raise BackendError(
                    "DeepSeek 出错：dsh: SERVER: Service is too busy. We advise "
                    "users to temporarily switch to alternative LLM API service "
                    "providers.")

        original = dsh_module.RETRY_PAUSE
        dsh_module.RETRY_PAUSE = 0.01
        try:
            backend = Fake({"command": ["dsh"], "timeout_seconds": 5}, {})
            with self.assertRaises(BackendError):
                backend.run("你好", "r1")
        finally:
            dsh_module.RETRY_PAUSE = original
        # Bounded, and small enough to be quick.
        self.assertLessEqual(len(calls), dsh_module.MAX_ATTEMPTS)
        self.assertGreaterEqual(len(calls), 2)

    def test_the_working_directory_is_resolved_against_the_config_folder(self):
        """A relative setting must not depend on where the host was started.

        The CLI records the session under the directory it ran in and
        refuses to resume a session recorded elsewhere, so the folder has
        to be the same every time.
        """
        with tempfile.TemporaryDirectory() as folder:
            workspace = os.path.join(folder, "workspace")
            os.makedirs(workspace)
            backend = DshBackend({"cwd": "workspace", "_root": folder}, {})
            resolved = backend.working_directory()
            self.assertTrue(os.path.isabs(resolved))
            self.assertEqual(resolved, os.path.abspath(workspace))

    def test_a_missing_working_directory_is_not_used(self):
        backend = DshBackend({"cwd": "nowhere-at-all", "_root": "/tmp"}, {})
        self.assertIsNone(backend.working_directory())


if __name__ == "__main__":
    unittest.main()
