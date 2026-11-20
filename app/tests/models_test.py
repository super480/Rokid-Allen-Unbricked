"""The model switch: how each CLI is told which model to use.

Two CLIs, two spellings:

* Hermes takes ``--model <name>`` on the command line.
* DSH has no such flag. The choice travels in a patch file that replaces
  the profile's ``agent-default-model`` entry.

The patch *replaces* that entry rather than merging into it, so a patch
without a provider leaves the plugin unable to start. That failure is
silent until a turn is sent, so it is pinned down here.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from backends import models as model_catalog  # noqa: E402
from backends.dsh import DshBackend  # noqa: E402
from backends.hermes import HermesBackend  # noqa: E402


class HermesModelTest(unittest.TestCase):
    def test_the_model_reaches_the_command_line(self):
        backend = HermesBackend({"model": "some-model"}, None)
        argv = backend.build_argv("你好", "")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "some-model")

    def test_without_a_model_no_flag_is_added(self):
        backend = HermesBackend({}, None)
        self.assertNotIn("--model", backend.build_argv("你好", ""))

    def test_switching_the_model_changes_the_command_line(self):
        backend = HermesBackend({}, None)
        backend.switch_model("another-model")
        self.assertEqual(backend.current_model(), "another-model")
        self.assertIn("another-model", backend.build_argv("你好", ""))

    def test_an_empty_model_falls_back_to_the_configured_one(self):
        backend = HermesBackend({"model": "picked"}, None)
        backend.switch_model("")
        self.assertEqual(backend.model, "")


class DshModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_patch_carries_the_model(self):
        backend = DshBackend({"model": "deepseek-chat", "_root": self.root}, None)
        argv = backend.build_argv("你好", "")
        self.assertIn("--patch", argv)
        path = Path(argv[argv.index("--patch") + 1])
        text = path.read_text(encoding="utf-8")
        self.assertIn("model: deepseek-chat", text)

    def test_the_patch_always_carries_the_provider(self):
        # A patch replaces the whole entry, so a patch without a provider
        # leaves the plugin unable to start.
        backend = DshBackend({"model": "deepseek-chat", "_root": self.root}, None)
        argv = backend.build_argv("你好", "")
        path = Path(argv[argv.index("--patch") + 1])
        text = path.read_text(encoding="utf-8")
        self.assertIn("provider: %s" % model_catalog.DSH_DEFAULT_PROVIDER, text)

    def test_the_patch_comes_before_the_profile_flags(self):
        # "dsh --patch x headless" is rejected: the launcher has not been
        # told which profile to boot yet.
        backend = DshBackend({"model": "deepseek-chat", "_root": self.root}, None)
        argv = backend.build_argv("你好", "")
        self.assertLess(argv.index("headless"), argv.index("--patch"))
        self.assertLess(argv.index("--patch"), argv.index("--json"))

    def test_without_a_model_no_patch_is_passed(self):
        backend = DshBackend({"_root": self.root}, None)
        argv = backend.build_argv("你好", "")
        self.assertNotIn("--patch", argv)

    def test_the_session_id_still_works(self):
        backend = DshBackend({"model": "deepseek-chat", "_root": self.root}, None)
        argv = backend.build_argv("你好", "sid-1")
        self.assertIn("--session-id", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], "sid-1")

    def test_the_default_model_is_reported(self):
        backend = DshBackend({}, None)
        self.assertEqual(backend.current_model(), model_catalog.DSH_DEFAULT_MODEL)

    def test_a_picked_model_is_reported(self):
        backend = DshBackend({"model": "deepseek-reasoner"}, None)
        self.assertEqual(backend.current_model(), "deepseek-reasoner")


class PatchFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_patch_is_written_atomically(self):
        target = Path(self.tmp.name) / "sub" / "model.patch.yml"
        model_catalog.write_dsh_patch(target, "deepseek-chat")
        text = target.read_text(encoding="utf-8")
        self.assertIn("- id: agent-default-model", text)
        self.assertIn("model: deepseek-chat", text)
        # No leftover temporary file.
        self.assertEqual([p.name for p in target.parent.iterdir()], ["model.patch.yml"])

    def test_a_chosen_provider_is_used(self):
        target = Path(self.tmp.name) / "model.patch.yml"
        model_catalog.write_dsh_patch(target, "m", "some-provider")
        self.assertIn("provider: some-provider",
                      target.read_text(encoding="utf-8"))


class CodexModelTest(unittest.TestCase):
    """The catalogue the console offers for Codex.

    The old list was hard-coded and went stale: it offered "gpt-5", a name
    the signed-in account cannot reach, so picking it made every turn fail
    with model_not_found. The catalogue now comes from the CLI's own cache.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "models_cache.json"
        self.patcher = mock.patch.object(
            model_catalog, "CODEX_MODELS_CACHE", self.cache)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_the_list_comes_from_the_cli_cache(self):
        import json
        self.cache.write_text(json.dumps({
            "models": [
                {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol"},
                {"slug": "gpt-6-luna", "display_name": "GPT-6-Luna"},
                {"slug": "codex-auto-review", "display_name": "Auto Review"},
            ]}), encoding="utf-8")
        self.assertEqual(model_catalog.codex_models(),
                         ["gpt-6.1-sol", "gpt-6-luna"])

    def test_a_broken_cache_falls_back(self):
        self.cache.write_text("not json", encoding="utf-8")
        self.assertEqual(model_catalog.codex_models(),
                         list(model_catalog.CODEX_FALLBACK_MODELS))

    def test_a_missing_cache_falls_back(self):
        self.assertEqual(model_catalog.codex_models(),
                         list(model_catalog.CODEX_FALLBACK_MODELS))

    def test_the_fallback_never_offers_an_unreachable_model(self):
        # "gpt-5" was in the old hard-coded list and every turn failed with
        # model_not_found. It must never come back through the fallback.
        self.assertNotIn("gpt-5", model_catalog.codex_models())


if __name__ == "__main__":
    unittest.main()
