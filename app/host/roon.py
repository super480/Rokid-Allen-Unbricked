#!/usr/bin/env python3
"""Client for the local Roon bridge sidecar (``app/roon-bridge``).

The sidecar is a Node Roon extension; the Python host only speaks HTTP to
it on 127.0.0.1. Every call degrades to a quiet "offline" answer when the
sidecar is not running, so the rest of the bridge keeps working without
Roon installed, authorised or even reachable.

The console's "voice control target" (auto / local / roon) lives here too:
it decides whether a spoken "暂停" drives the speaker's own MediaPlayer or
the Roon zone.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8766"
TARGETS = ("auto", "local", "roon")

OFFLINE = {"online": False, "stage": "offline", "core": None,
           "zones": [], "zone": None, "error": "", "updated_at": 0,
           "settings": {}, "socket": False, "paired": False,
           "authorised": False}

# The console switch lives in the same runtime file as the routing target.
# A fresh install has no Roon Core: the integration starts enabled (the
# sidecar quietly degrades to offline), and can be switched off entirely so
# no Node process, no polling and no Roon voice routing happens at all.
STATE_FILE = "roon-target.json"


class RoonClient:
    def __init__(self, base_url=None, state_path=None, timeout=1.5):
        self.base_url = (base_url or os.environ.get("ROON_BRIDGE_URL")
                         or DEFAULT_URL).rstrip("/")
        self.timeout = timeout
        self.state_path = Path(state_path) if state_path else None
        self.lock = threading.Lock()
        self.last = dict(OFFLINE)
        self.target = "auto"
        self.zone_id = ""
        self.enabled = True
        self._load()

    # -- console settings -------------------------------------------------
    def _load(self):
        if not self.state_path:
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("target") in TARGETS:
            self.target = data["target"]
        self.zone_id = str(data.get("zone_id") or "")
        self.enabled = data.get("enabled") is not False

    def _save(self):
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({"enabled": self.enabled,
                            "target": self.target, "zone_id": self.zone_id},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass

    def set_enabled(self, value):
        """Console power switch. Persisted so a host restart keeps it."""
        with self.lock:
            self.enabled = bool(value)
            self.last = dict(OFFLINE)
            self._save()
        return self.status()

    def configure(self, target=None, zone_id=None, host=None, port=None):
        """Change the host-side routing and/or the Core address."""
        with self.lock:
            if target in TARGETS:
                self.target = target
            if zone_id is not None:
                self.zone_id = str(zone_id or "")
            self._save()
            current_target, current_zone = self.target, self.zone_id
        # The Core address and the selected zone belong to the sidecar.
        payload = {}
        if host:
            payload["host"] = str(host)
        if port:
            try:
                payload["port"] = int(port)
            except (TypeError, ValueError):
                pass
        if zone_id is not None:
            payload["zone_id"] = str(zone_id or "")
        if payload and self.enabled:
            try:
                self._request("/settings", payload, timeout=3)
            except Exception:
                # status() below reports the sidecar as offline instead.
                pass
        status = self.status()
        status["target"] = current_target
        status["zone_id"] = current_zone
        return status

    # -- HTTP --------------------------------------------------------------
    def _request(self, path, body=None, timeout=None):
        url = self.base_url + path
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as reply:
            raw = reply.read()
            return reply.headers.get("Content-Type", ""), raw

    def status(self):
        """Merged sidecar + host state. Never raises.

        A disabled integration returns immediately: there is no sidecar to
        ask, and waiting for the connect timeout would slow the console.
        """
        with self.lock:
            enabled = self.enabled
            target = self.target
            zone_id = self.zone_id
        if not enabled:
            merged = dict(OFFLINE)
            merged["stage"] = "disabled"
            with self.lock:
                self.last = merged
        else:
            try:
                _, raw = self._request("/status")
                upstream = json.loads(raw.decode("utf-8", "replace"))
                if not isinstance(upstream, dict):
                    raise ValueError("bad status payload")
                merged = dict(upstream)
                merged["online"] = bool(upstream.get("online", True))
                merged.setdefault("stage", "offline")
                merged.setdefault("zones", [])
                merged.setdefault("zone", None)
            except Exception as error:
                merged = dict(OFFLINE)
                merged["error"] = str(error)[:120]
            with self.lock:
                self.last = merged
        merged["enabled"] = enabled
        merged["target"] = target
        merged["zone_id"] = zone_id
        return merged

    def control(self, action, value=None, zone_id=None):
        """Send one transport/volume action. Never raises."""
        if not self.enabled:
            return {"ok": False, "error": "Roon 集成已关闭"}
        body = {"action": str(action)}
        if value is not None:
            body["value"] = value
        with self.lock:
            wanted = zone_id or self.zone_id
        if wanted:
            body["zone_id"] = wanted
        try:
            _, raw = self._request("/control", body, timeout=3)
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception as error:
            return {"ok": False, "error": str(error)[:120]}

    def image(self, key, width=480, height=480):
        """Return (bytes, content_type) or None. Used for the device screen."""
        if not key:
            return None
        query = urllib.parse.urlencode({"key": key, "w": width, "h": height})
        try:
            content_type, raw = self._request("/image?" + query, timeout=6)
            if not raw or raw[:1] == b"{":
                return None
            return raw, (content_type.split(";")[0] or "image/jpeg")
        except Exception:
            return None

    # -- what to show on the speaker --------------------------------------
    @staticmethod
    def now_playing(status):
        """The compact shape pushed to the device, or None."""
        zone = status.get("zone") if isinstance(status, dict) else None
        if not isinstance(zone, dict):
            return None
        return {
            "zone_id": str(zone.get("zone_id") or ""),
            "zone_name": str(zone.get("display_name") or "Roon"),
            "title": str(zone.get("title") or ""),
            "artist": str(zone.get("artist") or ""),
            "album": str(zone.get("album") or ""),
            "state": str(zone.get("state") or "stopped"),
            "seek_position": float(zone.get("seek_position") or 0),
            "length": float(zone.get("length") or 0),
            "image_key": str(zone.get("image_key") or ""),
        }

    def routed_to_roon(self, audio_state=""):
        """True when a spoken playback command should drive Roon."""
        with self.lock:
            if not self.enabled:
                return False
            target = self.target
            last = self.last
        if target == "local":
            return False
        if not last.get("online") or last.get("stage") != "ready":
            return False
        zone = last.get("zone") or {}
        if target == "roon":
            return True
        # auto: our own MediaPlayer always wins while it is playing.
        if audio_state in ("playing", "paused"):
            return False
        return zone.get("state") in ("playing", "paused", "loading")


class SidecarPower:
    """Manage only the Node process started by this Host."""
    def __init__(self, app_root):
        self.folder = Path(os.environ.get("ROKID_ROON", Path(app_root) / "roon-bridge"))
        self.state = Path(os.environ.get("ROKID_DATA", Path(app_root) / "runtime")) / "roon"
        self.process = None
        self.lock = threading.Lock()

    def apply(self, enabled):
        with self.lock:
            if self.process and self.process.poll() is not None:
                self.process = None
            if enabled and not self.process:
                node = shutil.which("node")
                if not node or not (self.folder / "node_modules").is_dir():
                    raise ValueError("请先安装可选 Roon 扩展及 Node.js。")
                self.state.mkdir(parents=True, exist_ok=True)
                self.process = subprocess.Popen([node, str(self.folder / "index.js")],
                    env=dict(os.environ, ROON_STATE_DIR=str(self.state)), cwd=self.folder)
            elif not enabled and self.process:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
                self.process = None
