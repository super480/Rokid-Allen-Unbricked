"""Browser sign-in for WorkBuddy, the way the old app did it.

The old WorkBuddy app never asked the user to paste an API key. It opened
the browser, let the user log in to their WorkBuddy account there, and
turned that login into a token the app could use. The same three steps are
used here, unchanged in shape:

1. ask the gateway for a login state --
   ``POST {base}/v2/plugin/auth/state?platform=workbuddy``
   which answers with a ``state`` and the ``authUrl`` the user opens;
2. the user signs in at that URL in a browser;
3. swap the state for a credential --
   ``GET {base}/v2/plugin/auth/token?state={state}``
   which answers with an access token and a refresh token.

The account's name is then read with the token, the same way the old app
did it: ``GET {base}/v2/plugin/login/account``.

Two regions exist and they do not mix. The domestic gateway issues tokens
that only the domestic gateway accepts, and the international one issues
tokens for its own brand domains. A token therefore belongs to the region
that issued it, and the choice has to be made *before* the state is asked
for -- not after, when the token is already bound.

Only the standard library is used, like the rest of the host.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

# The plugin API prefix both regions share.
API_PREFIX = "/v2/plugin"

# The domestic gateway. Single-homed: there is no second domestic domain.
CN_BASE = "https://www.codebuddy.cn"

# The international gateway. The desktop app lives here; the brand domains
# ``workbuddy.ai`` and ``codebuddy.ai`` issue separate keys and the gateway
# refuses one issued by the other, so the caller may pin a base.
GLOBAL_BASE = "https://www.workbuddy.ai"

# The regions, in the order the console offers them.
REGIONS = ("cn", "global")

# How long one login may take before it is considered failed.
LOGIN_TTL_SECONDS = 600

# How long to wait for the gateway when asking it for a state or a token.
REQUEST_TIMEOUT = 30


def normalize_base(base):
    """Trim a base url and drop its trailing slashes.

    No scheme is invented: a base without one is a configuration mistake and
    is reported as such rather than silently turned into something else.
    """
    return str(base or "").strip().rstrip("/")


def base_for(region, explicit=None):
    """The gateway a region signs in against.

    ``explicit`` pins the international base, which is what a user on a
    brand domain needs: a token issued at one domain is refused by the
    other, so the choice must be made before the state is issued.
    """
    if region == "cn":
        return CN_BASE
    pinned = normalize_base(explicit)
    return pinned or GLOBAL_BASE


def _envelope_data(document):
    """The payload inside WorkBuddy's response envelope.

    The gateway wraps its answers in ``{code, msg, data}`` and answers 200
    even when the inner code says no. The payload may sit under ``data`` or
    come flat, so both are accepted.
    """
    if not isinstance(document, dict):
        return {}
    data = document.get("data")
    if isinstance(data, dict):
        return data
    return document


def _envelope_ok(document):
    """Whether the envelope reports success.

    The inner code is 0 on success and 200 on some deployments; anything
    else means the gateway refused.
    """
    if not isinstance(document, dict):
        return False
    code = document.get("code")
    if isinstance(code, bool):
        return False
    if isinstance(code, int):
        return code in (0, 200)
    # No code at all means the body is the payload itself.
    return True


def first_string(data, names):
    """The first of ``names`` that holds a non-empty string."""
    if not isinstance(data, dict):
        return ""
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def optional_string(data, names):
    """The first of ``names`` that holds a non-empty string, or None."""
    value = first_string(data, names)
    return value if value else None


def _post_json(url, body, timeout=REQUEST_TIMEOUT):
    """POST a JSON body and return the parsed answer, or None on failure."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body or {}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _get_json(url, headers=None, timeout=REQUEST_TIMEOUT):
    """GET a JSON document, or None when it cannot be read."""
    request = urllib.request.Request(
        url,
        headers=headers or {"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


class WorkBuddyLogin:
    """One sign-in in progress.

    The session lives in memory only: a restart cancels the login, and the
    state is worthless once the credential has been fetched.
    """

    def __init__(self, login_id, region, base, state, auth_url):
        self.login_id = login_id
        self.region = region
        self.base = base
        self.state = state
        self.auth_url = auth_url
        self.expires_at = time.time() + LOGIN_TTL_SECONDS
        self.done = False
        self.error = ""
        self.account = None

    def expired(self):
        return time.time() > self.expires_at


class WorkBuddyOAuth:
    """Drives the browser sign-in and keeps the sessions."""

    def __init__(self, base=None, region="cn", fetch=None):
        self.region = region if region in REGIONS else "cn"
        self.base = base_for(self.region, base)
        self._fetch = fetch
        self._sessions = {}
        self._counter = 0

    # -- starting a login ----------------------------------------------------

    def start(self, region=None, base=None):
        """Ask the gateway for a login state.

        Returns the URL the user opens. The answer is a dict with either
        ``auth_url`` or ``error`` in it -- a refusal is reported, never
        thrown, so the console can show the reason next to the button.
        """
        region = region if region in REGIONS else self.region
        chosen = base_for(region, base)
        document = self._post(chosen + API_PREFIX + "/auth/state?platform=workbuddy")
        if document is None:
            return {"error": "连不上 WorkBuddy 授权服务（%s）" % chosen}
        if not _envelope_ok(document):
            return {"error": "WorkBuddy 拒绝了授权请求：%s"
                    % (document.get("msg") or "未知原因")}
        data = _envelope_data(document)
        state = first_string(data, ["state", "State"])
        if not state:
            return {"error": "WorkBuddy 没有给出登录状态。"}
        auth_url = first_string(data, ["authUrl", "auth_url", "url", "Url"])
        if not auth_url:
            auth_url = "%s/login?state=%s" % (chosen, urllib.parse.quote(state))

        self._counter += 1
        login_id = "wbo_%d_%s" % (self._counter, state[:8])
        session = WorkBuddyLogin(login_id, region, chosen, state, auth_url)
        self._sessions[login_id] = session
        return {
            "login_id": login_id,
            "auth_url": auth_url,
            "region": region,
            "expires_in": LOGIN_TTL_SECONDS,
        }

    # -- finishing a login ---------------------------------------------------

    def poll(self, login_id):
        """Check whether the user finished signing in.

        Returns ``done`` plus either the account or an error. While the
        user is still in the browser the answer is ``done: False`` with no
        other keys, so the caller can simply keep polling.
        """
        session = self._sessions.get(str(login_id or ""))
        if session is None:
            return {"done": True, "error": "登录会话不存在，请重新发起授权。"}
        if session.done:
            return self._terminal(session)
        if session.expired():
            session.done = True
            session.error = "登录超时了，请重新发起授权。"
            self._sessions.pop(session.login_id, None)
            return {"done": True, "error": session.error}

        url = ("%s%s/auth/token?state=%s"
               % (session.base, API_PREFIX, urllib.parse.quote(session.state)))
        document = self._get(url)
        if document is None:
            # The gateway is unreachable; that is not the user's fault and
            # the login is still alive, so the next poll may still succeed.
            return {"done": False}
        if not _envelope_ok(document):
            # A refusal here usually means "not signed in yet".
            return {"done": False}
        data = _envelope_data(document)
        access_token = first_string(data, ["accessToken", "access_token", "AccessToken"])
        if not access_token:
            return {"done": False}

        session.done = True
        try:
            session.account = self._collect_account(session, access_token, data)
        except Exception as error:  # pragma: no cover - defensive
            session.error = "读取账号资料失败：" + str(error)
            self._sessions.pop(session.login_id, None)
            return {"done": True, "error": session.error}
        self._sessions.pop(session.login_id, None)
        return {"done": True, "account": session.account}

    @staticmethod
    def _terminal(session):
        if session.account is not None:
            return {"done": True, "account": session.account}
        return {"done": True, "error": session.error or "登录没有结果。"}

    def _collect_account(self, session, access_token, token_data):
        """Read the account profile and assemble what the host needs."""
        headers = {
            "Authorization": "Bearer " + access_token,
            "Accept": "application/json",
        }
        domain = first_string(token_data, ["domain", "Domain"])
        if domain:
            headers["X-Domain"] = domain

        profile = {}
        document = self._get(session.base + API_PREFIX + "/login/account",
                             headers=headers)
        if isinstance(document, dict):
            profile = _envelope_data(document)

        refresh_token = first_string(token_data, [
            "refreshToken", "refresh_token", "RefreshToken"])
        expires_at = first_string(token_data, ["expiresAt", "expires_at"])
        account = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "domain": domain,
            "region": session.region,
            # The gateway the sign-in went through. It is kept apart from the
            # model gateway: they are different hosts.
            "auth_base": session.base,
            "uid": first_string(profile, ["uid", "Uid", "userId", "UserID"]),
            "nickname": optional_string(profile, ["nickname", "NickName",
                                                 "nickName", "ScreenName"]),
            "uin": optional_string(profile, ["uin", "Uin"]),
            "enterprise_id": optional_string(profile, ["enterpriseId",
                                                       "EnterpriseId"]),
        }
        if expires_at:
            account["expires_at"] = expires_at
        return account

    # -- refreshing ----------------------------------------------------------

    def refresh(self, credential, region=None, base=None):
        """Trade a refresh token for a new access token.

        Returns the new credential, or None when it could not be refreshed.
        The gateway's refresh endpoint lives beside the token endpoint; the
        old app used the same shape.
        """
        refresh_token = str((credential or {}).get("refresh_token") or "").strip()
        if not refresh_token:
            return None
        chosen = normalize_base(base) or normalize_base(
            (credential or {}).get("auth_base")) or normalize_base(
                (credential or {}).get("base_url")) or base_for(
            region or self.region)
        url = "%s%s/auth/token" % (chosen, API_PREFIX)
        body = {"refreshToken": refresh_token, "grantType": "refresh_token"}
        document = self._post(url, body)
        if document is None:
            return None
        if not _envelope_ok(document):
            return None
        data = _envelope_data(document)
        access_token = first_string(data, ["accessToken", "access_token",
                                           "AccessToken"])
        if not access_token:
            return None
        updated = dict(credential or {})
        updated["access_token"] = access_token
        new_refresh = first_string(data, ["refreshToken", "refresh_token",
                                          "RefreshToken"])
        if new_refresh:
            updated["refresh_token"] = new_refresh
        expires_at = first_string(data, ["expiresAt", "expires_at"])
        if expires_at:
            updated["expires_at"] = expires_at
        updated["auth_base"] = chosen
        return updated

    # -- the transport -------------------------------------------------------

    def _post(self, url, body=None):
        if self._fetch is not None:
            return self._fetch(url, body if body is not None else {})
        return _post_json(url, body or {})

    def _get(self, url, headers=None):
        if self._fetch is not None:
            return self._fetch(url, None, headers)
        return _get_json(url, headers)
