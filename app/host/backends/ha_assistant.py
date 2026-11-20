"""The sixth assistant: 小家, HA's own conversation agent.

Unlike every other backend this one has no local brain -- it hands the
whole sentence to Home Assistant's ``/api/conversation/process`` and
speaks the agent's reply. The connection settings are shared with the
HomeAssistantService (REGISTRY.config_section), so there is exactly one
HA address/token in the config, not a second copy.

Turns keep ``conversation_id`` for a short window, so "哪个灯？" ->
"茶几灯" works the same way as the HA voice gate's clarify window. The
state is per backend instance: a host restart starts a fresh conversation.
"""
from __future__ import annotations

import time

from backends.base import Backend, BackendError, report
from homeassistant.client import HaClient, HaError

# Keep the HA session across turns for this long, then start fresh.
SESSION_WINDOW = 300.0


def _reply_text(data: dict) -> str:
    """Pull the speakable reply out of a conversation/process payload."""
    if not isinstance(data, dict):
        return ""
    response = data.get("response")
    if not isinstance(response, dict):
        return ""
    speech = response.get("speech")
    if isinstance(speech, dict):
        plain = speech.get("plain")
        if isinstance(plain, dict):
            return str(plain.get("speech") or "").strip()
        if isinstance(plain, str):
            return plain.strip()
    return ""


class HaAssistantBackend(Backend):
    """Answer one turn through HA's conversation agent."""

    id = "xiaojia"
    name = "小家"

    def __init__(self, settings=None, state_store=None):
        self.settings = dict(settings or {})
        self._conversation_id = ""
        self._last_turn_at = 0.0
        # Raw conversation/process payload of the most recent turn. The
        # engine uses it to draw 小家's own feedback card (targets and
        # entity names), which a plain text reply cannot provide.
        self.last_payload = {}

    # -- plumbing ----------------------------------------------------------

    def _client(self) -> HaClient:
        base = str(self.settings.get("base_url") or "").rstrip("/")
        token = str(self.settings.get("token") or "")
        if not base or not token:
            raise BackendError("小家还没连上，先在设置 → 家居里填好地址和令牌")
        timeout = self.settings.get("request_timeout_seconds") or 15
        return HaClient(base, token, timeout=float(timeout),
                        transport=self.settings.get("transport"))

    def _session_id(self) -> str:
        now = time.monotonic()
        if not self.settings.get("keep_session", True):
            return ""
        if now - self._last_turn_at > SESSION_WINDOW:
            self._conversation_id = ""
        return self._conversation_id

    # -- contract -----------------------------------------------------------

    def run(self, text, request_id, on_progress=None):
        conversation_id = self._session_id()
        report(on_progress, "thinking", "小家正在想")
        try:
            data = self._client().process(
                str(text or "").strip(),
                language=str(self.settings.get("language") or "zh-CN"),
                agent_id=str(self.settings.get("agent_id") or ""),
                conversation_id=conversation_id)
        except HaError as error:
            report(on_progress, "error", error.message)
            raise BackendError(error.message) from error
        self.last_payload = data if isinstance(data, dict) else {}
        reply = _reply_text(data)
        if not reply:
            reply = "小家没有听懂，换个说法试试"
        self._conversation_id = str(
            (data.get("conversation_id") if isinstance(data, dict) else "")
            or conversation_id)
        self._last_turn_at = time.monotonic()
        report(on_progress, "done", "")
        return reply

    def health(self):
        base = str(self.settings.get("base_url") or "")
        token = str(self.settings.get("token") or "")
        if not base or not token:
            return False, "还没有填写 HA 地址或令牌"
        return True, "ok"

    # -- model menu ---------------------------------------------------------

    def models(self):
        """HA has one configured conversation agent; "default" means the
        instance's own default pipeline."""
        agent = str(self.settings.get("agent_id") or "").strip()
        return [agent] if agent else ["default"]

    def current_model(self):
        return str(self.settings.get("model")
                   or self.settings.get("agent_id") or "default")

    def switch_model(self, name):
        value = str(name or "").strip()
        self.settings["agent_id"] = "" if value == "default" else value
        self.settings["model"] = value
        return True
