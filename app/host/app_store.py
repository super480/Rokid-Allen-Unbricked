"""Persistent app availability; removing an entry keeps its saved settings."""
from __future__ import annotations
import threading

CATALOG = (
    ("codex_usage", "Codex 用量"), ("network_speed", "网络速度"),
    ("system_resources", "系统资源"), ("weather", "天气查询"),
    ("clock", "待机时钟"), ("bambu_print", "打印进度"),
)
APP_IDS = frozenset(row[0] for row in CATALOG)


def preview_file(key):
    return key + (".svg" if key in ("bambu_print", "codex_usage") else ".png")


def is_installed(engine, app_id):
    config = getattr(engine, "config", {}) or {}
    saved = ((config.get("app_store") or {}).get("installed") or {})
    return saved.get(app_id, app_id != "bambu_print") is not False


class AppStore:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()

    def status(self):
        with self.lock:
            return {"apps": [{"id": key, "name": name,
                              "installed": is_installed(self.engine, key),
                              "preview": "/app-previews/" + preview_file(key)}
                             for key, name in CATALOG]}

    def set_installed(self, app_id, installed):
        if app_id not in APP_IDS:
            raise ValueError("未知应用")
        if not isinstance(installed, bool):
            raise ValueError("installed 必须为布尔值")
        with self.lock:
            if is_installed(self.engine, app_id) == installed:
                return self.status()
            # Clock is an independent device app: disabling its package stops
            # standby rendering as well as its voice entry, without uninstalling.
            if app_id == "clock":
                self.engine.clock.set_available(installed)
            state = self.engine.config.setdefault("app_store", {}).setdefault("installed", {})
            previous = dict(state)
            state[app_id] = installed
            api = getattr(self.engine, "weather_api", None) if app_id == "weather" else None
            try:
                if api is not None:
                    api.start() if self.engine.weather.enabled else api.stop()
                self.engine._save_config(strict=True)
            except Exception:
                state.clear(); state.update(previous)
                if app_id == "clock":
                    self.engine.clock.set_available(not installed)
                if api is not None:
                    api.start() if self.engine.weather.enabled else api.stop()
                raise
            if not installed and app_id in ("codex_usage", "network_speed", "system_resources", "bambu_print"):
                current = self.engine.miniapps.current_command()
                if current and current.get("app_id") == app_id:
                    self.engine.miniapps.hide()
            return self.status()
