#!/usr/bin/env python3
"""The Netease Cloud Music account behind the online music source.

The audio itself is served by N.E.K.O. (``/api/music/play/netease/<id>``),
which keeps the account cookie in its own encrypted credential store. So
logging in here means writing the cookie to that store, not to this host.
Nothing secret is persisted in the bridge: the cookie travels from the
console straight to the local N.E.K.O. service.

N.E.K.O. only offers QR login for bilibili / xhh. Netease is cookie-based,
so the console asks for MUSIC_U (or a whole Cookie header) instead.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class NeteaseError(Exception):
    """A user-facing account error."""


class NeteaseAuth:
    PLATFORM = "netease"

    def __init__(self, base, timeout=4):
        self.base = str(base).rstrip("/")
        self.timeout = timeout

    # -- upstream ----------------------------------------------------------

    def _call(self, path, method="GET", payload=None):
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": "RokidBridge/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024)
        except urllib.error.HTTPError as error:
            raise NeteaseError("上游返回 %s" % error.code) from error
        except Exception as error:
            raise NeteaseError("连不上 N.E.K.O.：" + str(error)[:80]) from error
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as error:
            raise NeteaseError("上游返回的不是 JSON") from error

    # -- console API -------------------------------------------------------

    def status(self):
        """Never raise: the console must still work when N.E.K.O. is down."""
        try:
            payload = self._call("/api/auth/cookies/status")
        except NeteaseError as error:
            return {"online": False, "logged_in": False, "error": str(error),
                    "credential_page": self.credential_page()}
        row = (payload.get("data") or {}).get(self.PLATFORM) or {}
        return {"online": True,
                "logged_in": bool(row.get("has_cookies")),
                "cookies_count": int(row.get("cookies_count") or 0),
                "credential_state": str(row.get("credential_state") or ""),
                "credential_page": self.credential_page()}

    def login(self, cookie):
        value = self.normalize(cookie)
        payload = self._call("/api/auth/cookies/save", "POST",
                             {"platform": self.PLATFORM,
                              "cookie_string": value, "encrypt": True})
        if payload.get("success") is False:
            raise NeteaseError(str(payload.get("message") or "上游拒绝了这份 Cookie"))
        state = self.status()
        if not state.get("logged_in"):
            raise NeteaseError("上游没有保存成功，请检查 N.E.K.O. 的凭证中心")
        state["ok"] = True
        state["message"] = str(payload.get("message") or "已登录网易云音乐")
        return state

    def logout(self):
        payload = self._call("/api/auth/cookies/" + self.PLATFORM, "DELETE")
        if payload.get("success") is False:
            raise NeteaseError(str(payload.get("message") or "退出失败"))
        state = self.status()
        state["ok"] = True
        return state

    def credential_page(self):
        return self.base + "/cookies_login"

    # -- input -------------------------------------------------------------

    @staticmethod
    def normalize(cookie):
        value = str(cookie or "").strip().strip('"').strip("'")
        if not value:
            raise NeteaseError("先粘贴 MUSIC_U 或整段 Cookie")
        if "\n" in value or "\r" in value:
            raise NeteaseError("Cookie 不能换行")
        if len(value) > 8192:
            raise NeteaseError("Cookie 太长了")
        if "=" not in value:
            value = "MUSIC_U=" + value
        if "MUSIC_U" not in value:
            raise NeteaseError("Cookie 里没有 MUSIC_U 字段")
        return value


class QQMusicAuth(NeteaseAuth):
    """QQ 音乐账号，复用 N.E.K.O. 的同一套 Cookie 上游，只是 platform 不同。

    和网易云一样：Cookie 只发到本机 N.E.K.O. 的加密凭证库，本控制台不保存。
    QQ 的 Cookie 字段名与网易云不同（没有 MUSIC_U），所以登录时不强制要求
    某个特定字段，只要是一段非空、不换行的 Cookie 即可。
    """

    PLATFORM = "qqmusic"

    @staticmethod
    def normalize(cookie):
        value = str(cookie or "").strip().strip('"').strip("'")
        if not value:
            raise NeteaseError("先粘贴 QQ 音乐的 Cookie")
        if "\n" in value or "\r" in value:
            raise NeteaseError("Cookie 不能换行")
        if len(value) > 8192:
            raise NeteaseError("Cookie 太长了")
        return value
