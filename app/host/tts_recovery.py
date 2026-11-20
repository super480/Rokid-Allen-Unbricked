"""Brings the speaker's TTS engine back after it wedges.

The factory's TTS service (``com.rokid.service.rktts``) streams the audio
from Rokid's cloud. Cancelling a request while it is already in flight can
leave the engine in a state where every later request times out after
exactly twenty seconds -- the engine's own timeout -- and the device stops
speaking altogether. The engine never recovers on its own.

The service is declared PERSISTENT, so the system restarts it after the
process dies, and a fresh process starts with an empty engine. Killing it is
therefore enough to recover. The app on the device cannot do that itself: it
has no root, and the ``su`` binary refuses its uid. The host can, because it
drives the device over adb as root.
"""
from __future__ import annotations

import os

import logging
import subprocess
import time
from pathlib import Path

LOG = logging.getLogger("tts-recovery")

#: The factory's TTS service. Killing it makes the system start a fresh one.
FACTORY_TTS_PACKAGE = "com.rokid.service.rktts"

#: How long to wait for the fresh service to come up and reconnect.
RESTART_SETTLE_SECONDS = 6.0


def _adb_path(config):
    if os.environ.get("ROKID_ADB"):
        return Path(os.environ["ROKID_ADB"])
    """The adb binary. The tools directory is not part of the repository."""
    tools = config.get("adb_tools") or ""
    if tools:
        return Path(tools) / "platform-tools" / "adb"
    root = Path(__file__).resolve().parents[2]
    return root / "tools" / "platform-tools" / "adb"


class TtsRecovery:
    """Restarts the device's TTS engine when it stops speaking.

    The recovery is deliberately conservative: it only acts after the device
    reported several failures in a row, and it never runs twice for the same
    incident.
    """

    def __init__(self, config):
        self.config = config or {}
        self.device = self.config.get("device") or ""
        self.enabled = bool(self.config.get("tts_recovery", True))
        self._running = False
        self._last_action = 0.0

    def available(self):
        """True when the host can talk to the device at all."""
        if not self.enabled or not self.device:
            return False
        return _adb_path(self.config).exists()

    def _adb(self, *args, timeout=30):
        command = [str(_adb_path(self.config)), "-s", self.device, *args]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=timeout)
        except (OSError, subprocess.SubprocessError) as error:
            LOG.warning("adb failed: %s", error)
            return ""
        return (result.stdout or "") + (result.stderr or "")

    def _factory_pid(self):
        output = self._adb("shell", "ps")
        for line in output.splitlines():
            if FACTORY_TTS_PACKAGE in line:
                parts = line.split()
                # "USER PID PPID ..." -- the pid is the second column.
                if len(parts) > 1 and parts[1].isdigit():
                    return parts[1]
        return ""

    def restart(self):
        """Kill the factory's TTS service so the system starts a fresh one.

        Returns True when the engine was restarted. The system brings the
        service back on its own because it is PERSISTENT; the app on the
        device rebinds and replays whatever was lost.
        """
        if self._running:
            return False
        if not self.available():
            LOG.info("recovery unavailable")
            return False
        # One restart per incident. The device reports every failure, and a
        # restart takes seconds, so a second one would only add noise.
        now = time.time()
        if now - self._last_action < 60:
            return False
        self._last_action = now
        self._running = True
        try:
            pid = self._factory_pid()
            if not pid:
                LOG.info("factory TTS service is not running")
                return False
            LOG.warning("restarting factory TTS service (pid %s)", pid)
            self._adb("shell", "kill", "-9", pid)
            time.sleep(RESTART_SETTLE_SECONDS)
            new_pid = self._factory_pid()
            if new_pid and new_pid != pid:
                LOG.warning("factory TTS service restarted (pid %s)", new_pid)
                return True
            LOG.warning("factory TTS service did not come back")
            return False
        finally:
            self._running = False
