"""No device is contacted. Check write ordering at the public installer boundary."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "macos"))
import rokidctl as ctl


class FirstRunTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(ctl, "DATA", Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.device = Mock(ip="192.168.1.8")
        self.state = dict(identity="speaker-one", boot="boot-a", secure="1", port="")
        self.device.inspect.side_effect = lambda: self.state.copy()

    def test_readback_is_required(self):
        self.device.command.return_value = ""
        with self.assertRaisesRegex(ValueError, "回读失败"):
            ctl.guard(self.device, True)
        self.assertEqual({}, ctl.load_record())

    def test_unauthenticated_firmware_does_not_get_persistent_network_adb(self):
        self.state["secure"] = ""
        with self.assertRaisesRegex(ValueError, "密钥认证"):
            ctl.guard(self.device, True)
        self.device.command.assert_not_called()

    def test_idempotent_and_boot_verified_separately(self):
        self.device.command.return_value = "5555"
        ctl.guard(self.device, True)
        self.assertFalse(ctl.load_record()["reboot_verified"])
        self.state["port"] = "5555"
        self.device.command.reset_mock()
        ctl.guard(self.device, True)
        self.device.command.assert_not_called()
        self.state["boot"] = "boot-b"
        ctl.guard(self.device, True)
        self.assertTrue(ctl.load_record()["reboot_verified"])

    def test_install_requires_explicit_guard_for_this_device(self):
        with patch.object(ctl, "install") as install:
            with self.assertRaisesRegex(ValueError, "先输入"):
                ctl.serve(self.device, True)
            install.assert_not_called()
        self.state["port"] = "5555"
        ctl.guard(self.device, True)
        self.state["identity"] = "another-speaker"
        with self.assertRaises(ValueError):
            ctl.guard(self.device, False)

    def test_wrong_model_is_rejected_before_setprop(self):
        with patch.object(ctl, "resolve_adb", return_value="adb"):
            device = ctl.Device("192.168.1.8")
        with patch.object(device, "command", side_effect=["connected", "device", "Other", "19"]) as command:
            with self.assertRaisesRegex(ValueError, "API 19"):
                ctl.guard(device, True)
            self.assertNotIn("setprop", str(command.call_args_list))
