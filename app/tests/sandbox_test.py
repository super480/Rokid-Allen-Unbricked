"""CLI integration preserves the caller's execution policy."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from sandbox import clean_env
from backends.dsh import DshBackend


class PolicyTest(unittest.TestCase):
    def test_inherits_environment_without_mutating_the_source(self):
        base = {"PATH": "/usr/bin", "CODEBUDDY_BROKERED_FS_HOOK_ENABLED": "1"}
        self.assertEqual(clean_env(base), base)
        self.assertIsNot(clean_env(base), base)

    def test_default_dsh_policy(self):
        self.assertEqual(DshBackend({}, None).build_env()["DSH_PERMISSION_MODE"], "workspace-write")
