"""The Hermes backend: argument building, session handling and failure modes."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from backends.base import BackendError  # noqa: E402
from backends.hermes import HermesBackend  # noqa: E402


class HermesBackendTest(unittest.TestCase):
    def make(self, **settings):
        base = {
            "command": ["hermes", "chat", "--oneshot", "--format", "stream-json"],
            "cwd": "",
            "timeout_seconds": 30,
            "keep_session": True,
        }
        base.update(settings)
        return HermesBackend(base, state_store={})

    def test_argv_uses_oneshot_and_stream_json(self):
        backend = self.make()
        argv = backend.build_argv("你好", "")
        self.assertIn("--oneshot", argv)
        self.assertIn("--format", argv)
        self.assertIn("stream-json", argv)
        self.assertEqual(argv[-2:], ["--query", "你好"])

    def test_argv_resumes_a_kept_session(self):
        backend = self.make()
        argv = backend.build_argv("继续", "local-abc")
        self.assertIn("--resume", argv)
        self.assertIn("local-abc", argv)

    def test_argv_omits_resume_without_a_session(self):
        backend = self.make()
        self.assertNotIn("--resume", backend.build_argv("继续", ""))

    def test_the_session_is_stored_for_the_next_turn(self):
        store = {}
        backend = self.make()
        backend.state_store = store
        backend._remember_session("local-xyz")
        self.assertEqual(store["hermes_session"], "local-xyz")

    def test_an_empty_store_still_accepts_a_session(self):
        # An empty dict is falsy; a naive "if not self.state_store" check
        # would silently drop the session id.
        store = {}
        backend = self.make()
        backend.state_store = store
        backend._remember_session("local-xyz")
        self.assertEqual(store.get("hermes_session"), "local-xyz")

    def test_reset_forgets_the_session(self):
        store = {"hermes_session": "local-xyz"}
        backend = self.make()
        backend.state_store = store
        backend.reset_session()
        self.assertNotIn("hermes_session", store)

    def test_the_session_remembers_the_model_it_runs_on(self):
        """A resumed session keeps its launch model, so it is recorded."""
        store = {}
        backend = self.make(model="model-a")
        backend.state_store = store
        backend._remember_session("local-xyz")
        self.assertEqual(store["hermes_session"], "local-xyz")
        self.assertEqual(store["hermes_session_model"], "model-a")

    def test_a_session_from_another_model_is_not_resumed(self):
        """Resuming would answer with the old model, so it is dropped.

        Hermes refuses a --model that differs from the session's launch
        model, so the only way the new model takes effect is a fresh
        session.
        """
        store = {"hermes_session": "local-old", "hermes_session_model": "model-old"}
        backend = self.make(model="model-new")
        backend.state_store = store
        self.assertEqual(backend.session_for_model(), "")
        # The session is dropped, so the next turn starts a fresh one.
        self.assertNotIn("hermes_session", store)
        self.assertNotIn("hermes_session_model", store)
        # ... and that fresh session carries the model.
        self.assertIn("--model", backend.build_argv("你好", backend.session_for_model()))
        self.assertIn("model-new", backend.build_argv("你好", backend.session_for_model()))

    def test_a_session_of_the_same_model_is_resumed(self):
        store = {"hermes_session": "local-same", "hermes_session_model": "model-a"}
        backend = self.make(model="model-a")
        backend.state_store = store
        self.assertEqual(backend.session_for_model(), "local-same")
        # The same model keeps the conversation, so no --model is needed.
        self.assertNotIn("--model", backend.build_argv("继续", backend.session_for_model()))

    def test_a_session_of_another_provider_is_not_resumed(self):
        """A session belongs to the endpoint it was started on.

        Hermes refuses to resume it with another provider, so it has to
        be dropped rather than continued.
        """
        store = {"hermes_session": "local-old",
                 "hermes_session_model": "model-a",
                 "hermes_session_provider": "minimax-cn"}
        backend = self.make(model="model-a", provider="nous")
        backend.state_store = store
        self.assertEqual(backend.session_for_model(), "")
        self.assertNotIn("hermes_session", store)

    def test_the_provider_is_remembered_with_the_session(self):
        store = {}
        backend = self.make(model="model-a", provider="nous")
        backend.state_store = store
        backend._remember_session("local-xyz")
        self.assertEqual(store["hermes_session_provider"], "nous")

    def test_a_session_without_a_recorded_provider_is_not_resumed(self):
        """An older state file has no provider for its session."""
        store = {"hermes_session": "local-old", "hermes_session_model": "model-a"}
        backend = self.make(model="model-a", provider="nous")
        backend.state_store = store
        self.assertEqual(backend.session_for_model(), "")

    def test_switching_the_model_drops_the_session(self):
        """A new model needs a new session to take effect."""
        store = {"hermes_session": "local-old", "hermes_session_model": "model-old"}
        backend = self.make(model="model-old")
        backend.state_store = store
        backend.switch_model("model-new")
        self.assertEqual(backend.model, "model-new")
        self.assertNotIn("hermes_session", store)
        self.assertNotIn("hermes_session_model", store)

    def test_switching_to_the_same_model_keeps_the_session(self):
        """The conversation is not thrown away for nothing."""
        store = {"hermes_session": "local-same", "hermes_session_model": "model-a"}
        backend = self.make(model="model-a")
        backend.state_store = store
        backend.switch_model("model-a")
        self.assertEqual(store.get("hermes_session"), "local-same")

    def test_a_session_without_a_recorded_model_is_not_resumed(self):
        """An older state file has no model for its session.

        The session may well run on another model, so it is dropped
        rather than continued with the wrong one.
        """
        store = {"hermes_session": "local-ancient"}
        backend = self.make(model="model-a")
        backend.state_store = store
        self.assertEqual(backend.session_for_model(), "")
        self.assertNotIn("hermes_session", store)

    def test_the_provider_is_named_along_with_the_model(self):
        """The model name goes to the provider, so it has to be named.

        Hermes sends the model name to the provider it is currently on.
        An endpoint that does not know the name rejects it, and Hermes
        then falls back to a different model without saying so.
        """
        from backends import models as model_catalog
        original = model_catalog.hermes_model_endpoints
        model_catalog.hermes_model_endpoints = lambda: {"model-a": "nous"}
        try:
            backend = self.make(model="model-a")
            self.assertEqual(backend.provider, "nous")
            argv = backend.build_argv("你好", "")
            self.assertIn("--provider", argv)
            self.assertEqual(argv[argv.index("--provider") + 1], "nous")
        finally:
            model_catalog.hermes_model_endpoints = original

    def test_switching_the_model_switches_the_provider(self):
        from backends import models as model_catalog
        original = model_catalog.hermes_model_endpoints
        model_catalog.hermes_model_endpoints = lambda: {
            "model-a": "nous", "model-b": "minimax-cn"}
        try:
            backend = self.make(model="model-a")
            self.assertEqual(backend.provider, "nous")
            backend.switch_model("model-b")
            self.assertEqual(backend.provider, "minimax-cn")
        finally:
            model_catalog.hermes_model_endpoints = original

    def test_health_reports_the_missing_binary(self):
        backend = self.make(command=["definitely-not-installed-xyz"])
        ok, detail = backend.health()
        self.assertFalse(ok)
        self.assertIn("hermes", detail)

    def test_a_configured_binary_is_not_replaced_by_another_one(self):
        backend = self.make(command=["definitely-not-installed-xyz"])
        self.assertIsNone(backend.executable())

    def test_tool_labels_cover_the_common_tools(self):
        from backends.hermes import TOOL_LABELS
        self.assertEqual(TOOL_LABELS["terminal"], "执行命令")
        self.assertEqual(TOOL_LABELS["web_search"], "联网搜索")

    def test_a_dead_session_restarts_the_conversation(self):
        """A session Hermes no longer knows about must not lose the turn."""
        calls = []

        class Fake(HermesBackend):
            def _run_once(self, text, request_id, on_progress=None):
                calls.append(text)
                if len(calls) == 1:
                    raise BackendError("Hermes 出错：session not found")
                return "重新开始后的回答"

        backend = Fake({"command": ["hermes"], "timeout_seconds": 5}, {"hermes_session": "local-gone"})
        answer = backend.run("你好", "r1")
        self.assertEqual(answer, "重新开始后的回答")
        self.assertEqual(len(calls), 2)
        # The dead session must be dropped so the retry starts fresh.
        self.assertEqual(backend.state_store.get("hermes_session", ""), "")

    def test_a_queued_session_restarts_the_conversation(self):
        """A session that still owes an answer would queue the turn for ever."""
        calls = []

        class Fake(HermesBackend):
            def _run_once(self, text, request_id, on_progress=None):
                calls.append(text)
                if len(calls) == 1:
                    raise BackendError(
                        "Hermes 会话队列未清空：Queued admission: abc (waiting to run): 现在几点了")
                return "新会话的回答"

        backend = Fake({"command": ["hermes"], "timeout_seconds": 5},
                       {"hermes_session": "local-stuck"})
        answer = backend.run("你好", "r1")
        self.assertEqual(answer, "新会话的回答")
        self.assertEqual(len(calls), 2)
        self.assertEqual(backend.state_store.get("hermes_session", ""), "")

    def test_a_queued_line_is_recognised(self):
        from backends.hermes import QUEUED
        self.assertTrue(QUEUED.match(
            "Queued admission: abc (waiting to run): 现在几点了"))
        self.assertFalse(QUEUED.match("Session: local-abc"))
        self.assertFalse(QUEUED.match("现在几点了"))

    def test_other_failures_are_not_retried(self):
        calls = []

        class Fake(HermesBackend):
            def _run_once(self, text, request_id, on_progress=None):
                calls.append(text)
                raise BackendError("Hermes 出错：模型不可用")

        backend = Fake({"command": ["hermes"], "timeout_seconds": 5}, {})
        with self.assertRaises(BackendError):
            backend.run("你好", "r1")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
