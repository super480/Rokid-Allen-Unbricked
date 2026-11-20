"""The Codex backend and the GPT assistant wiring."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import config as config_module  # noqa: E402
from backends.codex import CodexBackend  # noqa: E402
from engine import Engine  # noqa: E402


class CodexBackendTest(unittest.TestCase):
    """The Codex CLI is driven in non-interactive mode."""

    def test_the_command_line_starts_a_new_thread(self):
        backend = CodexBackend({"command": ["codex", "exec", "--json"]})
        argv = backend.build_argv("做点什么", "")
        # The binary is resolved to its path, so only the flags are checked.
        self.assertEqual(argv[1:3], ["exec", "--json"])
        self.assertIn("--skip-git-repo-check", argv)
        self.assertNotIn("resume", argv)
        # The prompt is a positional argument: the CLI reads it from stdin
        # when none is given, and the host has nothing to send there.
        self.assertEqual(argv[-1], "做点什么")
        # A sentence that starts with a dash must not be read as a flag.
        self.assertIn("--", argv)

    def test_a_kept_thread_is_resumed(self):
        backend = CodexBackend({"command": ["codex", "exec", "--json"]})
        argv = backend.build_argv("接着说", "01a1a140-0b22-7e42-9542-9d8baa8d001b")
        self.assertIn("resume", argv)
        self.assertIn("01a1a140-0b22-7e42-9542-9d8baa8d001b", argv)
        # The flags come before the subcommand, or the CLI rejects them.
        self.assertLess(argv.index("--json"), argv.index("resume"))
        self.assertEqual(argv[-1], "接着说")

    def test_the_model_is_only_set_on_a_new_thread(self):
        # A resumed thread keeps the model it was created with, so passing
        # a different one would be ignored by the CLI.
        backend = CodexBackend({"command": ["codex", "exec", "--json"],
                                "model": "gpt-5-codex"})
        self.assertIn("--model", backend.build_argv("问题", ""))
        self.assertNotIn("--model", backend.build_argv("问题", "thread-1"))

    def test_a_missing_binary_is_reported(self):
        backend = CodexBackend({"command": ["/nowhere/codex"]})
        ok, detail = backend.health()
        self.assertFalse(ok)
        self.assertIn("codex", detail)

    def test_the_session_is_kept_between_turns(self):
        store = {}
        backend = CodexBackend({"command": ["codex", "exec", "--json"]},
                                store)
        backend._remember_thread("thread-9")
        self.assertEqual(store["codex_thread"], "thread-9")
        self.assertEqual(backend._stored_thread(), "thread-9")
        backend.reset_session()
        self.assertEqual(backend._stored_thread(), "")

    def test_the_events_are_turned_into_an_answer(self):
        backend = CodexBackend({"command": ["codex", "exec", "--json"]})
        events = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "我先看看"}},
            {"type": "item.completed",
             "item": {"type": "command_execution", "command": "ls"}},
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "答案是 42"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10}},
        ]
        states = []

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        answer = self._run_with_events(backend, events, states)
        self.assertEqual(answer, "答案是 42")
        # The thread id is remembered, so the next turn keeps the context.
        self.assertEqual(backend.thread_id, "thread-1")
        # A tool call is reported as progress, so the screen shows it.
        self.assertIn("working", states)

    def test_an_empty_answer_is_an_error(self):
        backend = CodexBackend({"command": ["codex", "exec", "--json"]})
        events = [{"type": "turn.started"},
                  {"type": "turn.completed", "usage": {}}]
        with self.assertRaises(Exception):
            self._run_with_events(backend, events, [])

    def test_a_failed_turn_says_why(self):
        """A failure has to name its cause, not the last progress line.

        The CLI reports a failed turn in a nested JSON string. When that is
        missed, the turn ends with no answer and the error is reported as
        whatever the CLI happened to print last, which is a progress
        message such as "Reading additional input from stdin...".
        """
        backend = CodexBackend({"command": ["codex", "exec", "--json"]})
        nested = json.dumps({
            "type": "error", "status": 400,
            "error": {"type": "invalid_request_error",
                      "message": "The 'gpt-5' model is not supported.",
                      "code": "model_not_found"}})
        events = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "item.completed",
             "item": {"type": "error", "message": "model not found"}},
            {"type": "error", "message": nested},
            {"type": "turn.failed", "error": {"message": nested}},
        ]
        with self.assertRaises(Exception) as caught:
            self._run_with_events(backend, events, [])
        message = str(caught.exception)
        self.assertIn("model is not supported", message)
        self.assertNotIn("stdin", message)

    def test_an_error_item_alone_is_enough_to_fail(self):
        """An error item with no turn.failed still has to be reported."""
        backend = CodexBackend({"command": ["codex", "exec", "--json"]})
        events = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "item.completed",
             "item": {"type": "error", "message": "the model is unknown"}},
            {"type": "turn.completed", "usage": {}},
        ]
        with self.assertRaises(Exception) as caught:
            self._run_with_events(backend, events, [])
        self.assertIn("the model is unknown", str(caught.exception))

    def test_a_nested_error_is_unwrapped(self):
        """The real reason sits two levels down in the CLI's JSON."""
        from backends.codex import readable_error
        nested = json.dumps({
            "type": "error", "status": 400,
            "error": {"message": "the model gpt-5 is not supported"}})
        self.assertEqual(readable_error(nested), "the model gpt-5 is not supported")
        self.assertEqual(readable_error("plain text"), "plain text")
        self.assertEqual(readable_error(""), "")

    def _run_with_events(self, backend, events, states):
        """Run the backend against a fixed event stream."""
        lines = "\n".join(json.dumps(e) for e in events) + "\n"

        class FakeProcess:
            def __init__(self):
                self.stdout = iter(lines.splitlines(True))
                self.stderr = iter(())

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        original = subprocess.Popen
        subprocess.Popen = lambda *a, **k: FakeProcess()
        try:
            return backend.run("问题", "req-1",
                               lambda state, detail="": states.append(state))
        finally:
            subprocess.Popen = original


class ChatGptAgentTest(unittest.TestCase):
    """The GPT assistant is a first-class assistant, like the others."""

    def test_it_is_enabled_and_has_a_backend(self):
        item = config_module.REGISTRY["codex"]
        self.assertTrue(item["enabled"])
        from backends import BACKENDS
        self.assertIn(item["backend"], BACKENDS)

    def test_its_design_folder_exists(self):
        # The console shows the character's face from this folder, so the
        # name in the registry has to match the folder on disk.
        folder = config_module.REGISTRY["codex"]["design"]
        root = Path(__file__).resolve().parents[2] / "design" / "avatars" / folder
        self.assertTrue((root / "avatar.png").exists(), str(root))
        for state in config_module.AVATAR_STATES:
            self.assertTrue((root / "states" / (state + ".png")).exists(),
                            "%s is missing" % state)

    def test_it_can_be_selected_and_switched_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = config_module.create_config(path)
            config["_root"] = tmp
            engine = Engine(config)
            try:
                self.assertEqual(engine.select("codex", announce=False), "codex")
                self.assertEqual(engine.selected, "codex")
                # It has a conversation of its own, like the other
                # assistants, so the console can show a thread per assistant.
                engine.log_transcript("user", "codex", "你好")
                engine.log_transcript("agent", "codex", "在的")
                threads = engine.threads()
                self.assertIn("codex", threads["threads"])
                rows = threads["threads"]["codex"]
                self.assertEqual([r["role"] for r in rows[-2:]], ["user", "agent"])
                self.assertEqual(rows[-1]["message"], "在的")
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
