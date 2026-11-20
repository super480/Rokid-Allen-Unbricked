"""Direct mode: the speaker answers on its own.

Two sources are possible:

* **assistant** -- reuse an existing assistant's model and tools. The
  conversation is kept under a session key of its own, so asking the
  speaker a question does not disturb the conversation the user is having
  with that assistant.
* **custom** -- call an OpenAI-compatible endpoint directly with an api
  key. The conversation history lives on this side, because such an
  endpoint keeps no state between calls.

Only the standard library is used, like the rest of the host.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from backends.base import Backend, BackendError, report

# The modes direct mode understands.
OFF = "off"
HYBRID = "hybrid"
FULL = "full"
MODES = (OFF, HYBRID, FULL)

# How many turns of a custom endpoint's conversation are kept. The history
# has to fit in the request, and an old answer is rarely still relevant.
MAX_HISTORY = 12


def normalise_mode(mode):
    """The mode, or "off" when the value is not one we know."""
    value = str(mode or "").strip().lower()
    return value if value in MODES else OFF


class OpenAiCompatBackend(Backend):
    """An OpenAI-compatible chat endpoint."""

    id = "direct"
    name = "直连"

    def __init__(self, settings, state_store=None):
        self.settings = settings or {}
        self.state_store = state_store
        self.base_url = str(self.settings.get("base_url") or "").strip()
        self.api_key = str(self.settings.get("api_key") or "").strip()
        self.model = str(self.settings.get("model") or "").strip()
        self.timeout = int(self.settings.get("timeout_seconds") or 60)
        self.session_key = str(self.settings.get("session_key") or "direct_messages")

    # -- configuration --------------------------------------------------------

    def health(self):
        if not self.base_url:
            return False, "还没填直连的接口地址"
        if not self.api_key:
            return False, "还没填直连的 api key"
        if not self.model:
            return False, "还没填直连的模型名"
        return True, self.base_url

    def endpoint(self):
        """The URL to post to.

        A base url usually ends in ``/v1``; some people paste the full
        endpoint instead. Both are accepted.
        """
        url = self.base_url.rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        return url + "/chat/completions"

    # -- conversation ---------------------------------------------------------

    def _messages(self):
        if self.state_store is None:
            return []
        value = self.state_store.get(self.session_key)
        return list(value) if isinstance(value, list) else []

    def _remember(self, messages):
        if self.state_store is None:
            return
        # Keep the history short: it is sent with every request.
        self.state_store[self.session_key] = messages[-MAX_HISTORY:]

    # -- running --------------------------------------------------------------

    def run(self, text, request_id, on_progress=None):
        ok, detail = self.health()
        if not ok:
            raise BackendError("直连还没配置好：" + detail)
        text = (text or "").strip()
        if not text:
            raise BackendError("没有听到问题内容。")

        messages = self._messages()
        messages.append({"role": "user", "content": text})
        report(on_progress, "thinking", "正在思考")

        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        request = urllib.request.Request(
            self.endpoint(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            body_text = ""
            try:
                body_text = error.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise BackendError("直连接口报错 %s %s" % (error.code, body_text))
        except urllib.error.URLError as error:
            raise BackendError("连不上直连接口：" + str(error.reason))
        except OSError as error:
            raise BackendError("直连接口出错：" + str(error))

        try:
            data = json.loads(payload)
        except ValueError:
            raise BackendError("直连接口返回的不是 JSON。")

        answer = self._answer_of(data)
        if not answer:
            raise BackendError("直连接口没有返回内容。")

        messages.append({"role": "assistant", "content": answer})
        self._remember(messages)
        report(on_progress, "speaking", "")
        report(on_progress, "done", "")
        return answer

    @staticmethod
    def _answer_of(data):
        """Pull the answer out of the response.

        The shape is the one OpenAI defines, but some servers put the text
        somewhere else, so a few places are tried before giving up.
        """
        if not isinstance(data, dict):
            return ""
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content.strip()
                    # Some servers send the text as a list of parts.
                    if isinstance(content, list):
                        parts = [part.get("text", "") for part in content
                                 if isinstance(part, dict)]
                        return "".join(parts).strip()
                text = first.get("text")
                if isinstance(text, str):
                    return text.strip()
        for key in ("output_text", "answer", "content"):
            value = data.get(key)
            if isinstance(value, str):
                return value.strip()
        return ""


class DirectBackend(Backend):
    """Answers a question without an assistant being selected.

    The work is done by the chosen source; this class only picks it and
    keeps the two conversations apart.
    """

    id = "direct"
    name = "直连"

    def __init__(self, settings, state_store=None):
        self.settings = settings or {}
        self.state_store = state_store
        self.source = str(self.settings.get("source") or "assistant").strip().lower()
        self.assistant = str(self.settings.get("assistant") or "hermes").strip()
        self._inner = None

    def inner(self):
        """The backend that actually answers.

        For the assistant source the existing backend is reused with a
        session key of its own, so the assistant's conversation stays
        untouched.
        """
        if self._inner is not None:
            return self._inner
        if self.source == "custom":
            settings = dict(self.settings.get("custom") or {})
            settings.setdefault("session_key", "direct_messages")
            self._inner = OpenAiCompatBackend(settings, self.state_store)
            return self._inner
        from backends import load_backend
        from config import agent_settings
        settings = dict(self.settings.get("assistant_settings") or {})
        settings["session_key"] = "direct_session"
        self._inner = load_backend(self.assistant, settings, self.state_store)
        return self._inner

    def switch_model(self, name):
        """Change the model the inner backend answers with.

        Direct mode keeps a conversation of its own, so the model is
        changed on the inner backend and that conversation is dropped: a
        resumed Hermes session keeps the model it was started with, so
        carrying it over would silently answer with the old model.
        """
        name = str(name or "").strip()
        if self.source == "custom":
            custom = self.settings.setdefault("custom", {})
            custom["model"] = name
            self._inner = None
            return name
        # The settings are a snapshot taken when the direct backend was
        # built; the model is updated in it so a rebuilt inner backend
        # gets it as well.
        assistant_settings = self.settings.setdefault("assistant_settings", {})
        assistant_settings["model"] = name
        if self._inner is not None:
            try:
                self._inner.switch_model(name)
                # The provider follows the model; a rebuilt inner backend
                # has to know it as well.
                assistant_settings["provider"] = getattr(self._inner, "provider", "")
            except Exception:
                pass
        return name

    def health(self):
        try:
            return self.inner().health()
        except BackendError as error:
            return False, str(error)

    def run(self, text, request_id, on_progress=None):
        return self.inner().run(text, request_id, on_progress)

    def cancel(self, request_id):
        if self._inner is None:
            return False
        return self._inner.cancel(request_id)

    def close(self):
        if self._inner is not None:
            self._inner.close()
