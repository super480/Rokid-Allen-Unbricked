"""Recovering the speaker's TTS engine after it wedges."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

import tts_recovery  # noqa: E402
from tts_recovery import FACTORY_TTS_PACKAGE, TtsRecovery  # noqa: E402


class FakeDevice:
    """A device that answers adb calls without a real speaker attached."""

    def __init__(self, pids=("100", "200")):
        self.pids = list(pids)
        self.calls = []

    def adb(self, *args, timeout=30):
        self.calls.append(list(args))
        if args[:2] == ("shell", "ps"):
            return "system %s 1 0 0 S %s\n" % (self.pids[0], FACTORY_TTS_PACKAGE)
        if args[:1] == ("shell",) and "kill" in args:
            # The service is gone; the system starts a new one.
            self.pids = self.pids[1:] or ["999"]
            return ""
        return ""

    def pid(self):
        return self.pids[0] if self.pids else ""


class RecoveryTest(unittest.TestCase):

    def _recovery(self, device):
        recovery = TtsRecovery({"device": "1.2.3.4:5555"})
        recovery.available = lambda: True  # the simulated device is present
        recovery._adb = device.adb
        recovery._factory_pid = device.pid
        # The real restart waits for the service to come back; shorten it.
        tts_recovery.RESTART_SETTLE_SECONDS = 0
        return recovery

    def test_it_finds_the_factory_service(self):
        device = FakeDevice()
        self.assertEqual(self._recovery(device)._factory_pid(), "100")

    def test_a_restart_kills_the_service(self):
        device = FakeDevice()
        self.assertTrue(self._recovery(device).restart())
        self.assertIn(["shell", "kill", "-9", "100"], device.calls)

    def test_a_restart_reports_the_new_process(self):
        device = FakeDevice(pids=("100", "200"))
        self.assertTrue(self._recovery(device).restart())
        self.assertEqual(device.pid(), "200")

    def test_it_is_unavailable_without_a_device(self):
        self.assertFalse(TtsRecovery({}).available())
        self.assertFalse(TtsRecovery({"device": ""}).available())

    def test_it_can_be_switched_off(self):
        recovery = TtsRecovery({"device": "1.2.3.4:5555",
                                "tts_recovery": False})
        self.assertFalse(recovery.available())

    def test_one_restart_per_incident(self):
        # The device reports every failure; a second restart right away
        # would only add noise.
        device = FakeDevice()
        recovery = self._recovery(device)
        self.assertTrue(recovery.restart())
        self.assertFalse(recovery.restart())


if __name__ == "__main__":
    unittest.main()
