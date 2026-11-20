"""WorkBuddy assistant backend.

巴蒂 answers through WorkBuddy's own gateway. The account is authorised the
way the old app authorised it: the user signs in to WorkBuddy in a browser,
and the credential that comes out of that sign-in is stored in a dedicated,
non-committed file (``workbuddy-auth.json``) so the secret never lands in
the shared config or the APK. See :mod:`backends.workbuddy_oauth` for the
three-step browser sign-in.

The gateway speaks an OpenAI-shaped protocol but only over a **stream**: a
non-stream request is refused with "Non-stream chat request is currently not
supported". The answer therefore arrives as server-sent events, and the
pieces have to be put back together here. The stream carries two separate
texts -- ``reasoning_content`` is the model thinking out loud and
``content`` is the answer -- and only the latter is spoken.

A token that has expired is refreshed with the stored refresh token rather
than sending the user back to the browser.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from backends.base import BackendError, report
from backends.direct import OpenAiCompatBackend
from backends.workbuddy_oauth import WorkBuddyOAuth

# The gateway that serves the models. A domestic credential is accepted
# here; an international one is refused with 401.
MODEL_BASE = "https://copilot.tencent.com"

# The models WorkBuddy serves. The list is the one the account can reach,
# so the menu never offers a model that would be refused.
WORKBUDDY_MODELS = (
    "hy4-preview",
    "hy3",
    "hy3-x",
    "deepseek-v4.1-flash",
    "deepseek-v4-pro",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "step-5-preview",
    "kimi-k3.1-pro",
    "kimi-k2.8-preview",
    "minimax-m3.1-flash",
    "mimo-v2.6-pro",
)

# The model used when nothing else is chosen.
DEFAULT_MODEL = "hy3"

# How many turns of the conversation are kept. The history travels with
# every request, so it has to stay short.
MAX_HISTORY = 12

# A token is refreshed this many seconds before it actually expires.
REFRESH_MARGIN = 120


class WorkBuddyBackend(OpenAiCompatBackend):
    """Answers through WorkBuddy's gateway, signed in by the browser."""

    id = "workbuddy"
    name = "巴蒂"

    def __init__(self, settings, state_store=None):
        # Start from whatever the config section says, then let the dedicated
        # authorisation file override it -- the token is a secret that does
        # not belong in the shared config.
        settings = dict(settings or {})
        super().__init__(settings, state_store)
        # Keep this assistant's conversation to itself, separate from the
        # speaker's direct path and from every other assistant.
        self.session_key = str(settings.get("session_key") or "workbuddy_messages")
        self.auth_path = self._auth_path(settings)
        self.credential = self._load_credential(self.auth_path)
        self.region = str(self.credential.get("region") or "cn").strip() or "cn"
        # Two different places are involved and they must not be mixed up:
        # the *authorisation* gateway (where the browser sign-in happens) and
        # the *model* gateway (where the answers come from). The first is
        # decided by the region, the second is a fixed host for the
        # domestic account.
        self.auth_base = str(self.credential.get("auth_base") or "").strip()
        self.base_url = str(self.credential.get("base_url")
                            or settings.get("base_url") or "").strip()
        self.api_key = str(self.credential.get("access_token")
                           or settings.get("api_key") or "").strip()
        self.model = str(self.credential.get("model")
                         or settings.get("model") or DEFAULT_MODEL).strip()
        try:
            self.timeout = int(settings.get("timeout_seconds") or 120)
        except (TypeError, ValueError):
            self.timeout = 120

    # -- authorisation file ---------------------------------------------------

    @staticmethod
    def _auth_path(settings):
        """Where the WorkBuddy authorisation lives.

        The file is named after the assistant id, so ``workbuddy`` becomes
        ``workbuddy-auth.json`` -- the path the repository's ``.gitignore``
        already keeps out of version control.
        """
        root = settings.get("_root") or ""
        name = "workbuddy-auth.json"
        if root:
            return os.path.join(str(root), name)
        return name

    @staticmethod
    def _load_credential(path):
        """Read the authorisation file, or return an empty dict when missing."""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_credential(self, credential):
        """Store a credential and adopt it."""
        data = dict(self.credential or {})
        data.update(credential or {})
        self.credential = data
        self.api_key = str(data.get("access_token") or "").strip()
        self.base_url = str(data.get("base_url") or "").strip()
        self.auth_base = str(data.get("auth_base") or "").strip()
        self.region = str(data.get("region") or self.region).strip() or "cn"
        if data.get("model"):
            self.model = str(data["model"]).strip()

    # -- capabilities ---------------------------------------------------------

    def health(self):
        if not self.api_key:
            return False, "还没登录：请在控制台用浏览器登录 WorkBuddy 账号"
        return True, ("%s · %s" % (self.nickname() or "WorkBuddy", self.model))

    def nickname(self):
        """The name of the signed-in account, when it is known."""
        return str(self.credential.get("nickname") or "").strip()

    def current_model(self):
        return self.model

    def switch_model(self, name):
        self.model = str(name or "").strip()
        return self.model

    def models(self):
        """The models this assistant can answer with.

        The chosen model comes first so it is the easy pick, then the rest of
        the catalogue WorkBuddy serves.
        """
        names = []
        if self.model:
            names.append(self.model)
        for name in WORKBUDDY_MODELS:
            if name not in names:
                names.append(name)
        return names

    # -- the gateway ----------------------------------------------------------

    def endpoint(self):
        """Where the chat request goes."""
        base = self.base_url or MODEL_BASE
        return base.rstrip("/") + "/v2/chat/completions"

    def _token_expired(self):
        """Whether the stored token is past its expiry (or nearly so)."""
        raw = self.credential.get("expires_at")
        if raw in (None, ""):
            return False
        try:
            expires = float(raw)
        except (TypeError, ValueError):
            return False
        # The gateway reports seconds since the epoch; a millisecond value is
        # possible too, so anything far in the future is divided down.
        if expires > 1e11:
            expires = expires / 1000.0
        return expires - REFRESH_MARGIN <= time.time()

    def _refresh(self):
        """Refresh the token, or give up and say the login is needed again."""
        refresh_token = str(self.credential.get("refresh_token") or "").strip()
        if not refresh_token:
            return False
        oauth = WorkBuddyOAuth(region=self.region)
        updated = oauth.refresh(self.credential, region=self.region)
        if not updated:
            return False
        self.save_credential(updated)
        self._write_credential()
        return True

    def _write_credential(self):
        """Write the credential back to the authorisation file."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.auth_path)),
                        exist_ok=True)
            temp = self.auth_path + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(self.credential, handle, ensure_ascii=False, indent=2)
            os.replace(temp, self.auth_path)
            try:
                os.chmod(self.auth_path, 0o600)
            except OSError:
                pass
        except OSError:
            pass

    def _headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": "Bearer " + self.api_key,
        }
        domain = str(self.credential.get("domain") or "").strip()
        if domain:
            headers["X-Domain"] = domain
        return headers

    # -- running --------------------------------------------------------------

    def run(self, text, request_id, on_progress=None):
        ok, detail = self.health()
        if not ok:
            raise BackendError("巴蒂还不能回答：" + detail)
        text = (text or "").strip()
        if not text:
            raise BackendError("没有听到任务内容。")
        report(on_progress, "thinking", "正在思考")

        messages = self._messages()
        messages.append({"role": "user", "content": text})

        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        # A token that is known to have expired is renewed before the request
        # is made, so a valid turn is not spent on a rejection.
        if self._token_expired():
            self._refresh()

        answer = ""
        for attempt in (0, 1):
            request = urllib.request.Request(
                self.endpoint(),
                data=payload,
                headers=self._headers(),
                method="POST",
            )
            try:
                answer = self._read_stream(request)
            except urllib.error.HTTPError as error:
                body_text = ""
                try:
                    body_text = error.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                if error.code == 401 and attempt == 0 and self._refresh():
                    # The token was refused after all; a refreshed one may
                    # work, so the turn is tried once more.
                    continue
                raise BackendError("巴蒂接口报错 %s %s" % (error.code, body_text))
            except urllib.error.URLError as error:
                raise BackendError("连不上巴蒂接口：" + str(error.reason))
            except OSError as error:
                raise BackendError("巴蒂接口出错：" + str(error))
            except BackendError:
                raise
            break

        if not answer:
            raise BackendError("巴蒂没有返回内容。")

        messages.append({"role": "assistant", "content": answer})
        self._remember(messages)
        report(on_progress, "speaking", "")
        report(on_progress, "done", "")
        return answer

    def _read_stream(self, request):
        """Read the server-sent events and return the answer.

        The stream mixes the model's thinking (``reasoning_content``) with
        its answer (``content``). Only the answer is returned; the thinking
        is dropped, because it is not something to say out loud.
        """
        parts = []
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        if data == "[DONE]":
                            break
                        continue
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    piece = self._content_of(event)
                    if piece:
                        parts.append(piece)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError):
            raise
        return "".join(parts).strip()

    @staticmethod
    def _content_of(event):
        """The answer text carried by one stream event."""
        if not isinstance(event, dict):
            return ""
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        delta = first.get("delta")
        if not isinstance(delta, dict):
            return ""
        content = delta.get("content")
        if isinstance(content, str):
            return content
        return ""
