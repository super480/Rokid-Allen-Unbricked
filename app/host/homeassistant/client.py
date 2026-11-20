"""HTTP client for the Home Assistant REST API.

Standard library only, like the rest of the host. The transport is
injectable so the unit tests never open a socket: every error class a
test asserts on is one this client actually distinguishes --

    HaNetworkError   cannot connect / timeout  (HA down, wrong host)
    HaAuthError      401 / 403                 (token revoked, no rights)
    HaNotFoundError  404                       (bad base_url or entity)
    HaProtocolError  5xx, non-JSON, oversized, unexpected shape

Writes never retry automatically: a timeout can land *after* the action
already ran, and a second call would flip the switch twice. Reads may
retry once (GET is safe).
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable

DEFAULT_TIMEOUT = 15
# /api/states on a large instance easily exceeds 1 MiB of JSON.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class HaError(Exception):
    """Base: a short Chinese message for TTS / console, nothing secret."""
    kind = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class HaNetworkError(HaError):
    kind = "network"


class HaAuthError(HaError):
    kind = "auth"


class HaNotFoundError(HaError):
    kind = "not_found"


class HaProtocolError(HaError):
    kind = "protocol"


# (method, url, headers, body, timeout) -> (status, body bytes)
Transport = Callable[[str, str, dict, bytes | None, float], tuple[int, bytes]]


def default_transport(method: str, url: str, headers: dict,
                      body: bytes | None, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        return error.code, error.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, socket.timeout, TimeoutError,
            ConnectionError, OSError) as error:
        raise HaNetworkError("连不上 Home Assistant：" + str(error.reason
            if isinstance(error, urllib.error.URLError) else error)[:120])


class HaClient:
    """One HA instance. Holds no state beyond url + token."""

    def __init__(self, base_url: str, token: str,
                 timeout: float = DEFAULT_TIMEOUT,
                 transport: Transport | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self._transport = transport or default_transport

    # -- plumbing ----------------------------------------------------------

    def _request(self, method: str, path: str,
                 payload: dict | None = None) -> Any:
        url = self.base_url + path
        headers = {"Authorization": "Bearer " + self.token}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        status, raw = self._transport(method, url, headers, body, self.timeout)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise HaProtocolError("返回数据过大，已中止读取")
        if status in (401, 403):
            raise HaAuthError("访问令牌无效或权限不足")
        if status == 404:
            raise HaNotFoundError("地址或实体不存在")
        if status >= 500:
            raise HaProtocolError("Home Assistant 内部错误 %d" % status)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HaProtocolError("返回内容不是 JSON")
        if status >= 400:
            message = ""
            if isinstance(data, dict):
                message = str(data.get("message") or data.get("error") or "")
            raise HaProtocolError(message or ("请求被拒绝 %d" % status))
        return data

    def _get(self, path: str) -> Any:
        # A read is safe to retry once; the second failure is real.
        try:
            return self._request("GET", path)
        except HaNetworkError:
            return self._request("GET", path)

    # -- read-only API -------------------------------------------------------

    def api_check(self) -> dict:
        """GET /api/. Proves reachability + auth. Never a service call."""
        data = self._get("/api/")
        if not isinstance(data, dict) or "message" not in data:
            raise HaProtocolError("响应缺少 message 字段")
        return {"message": str(data["message"])}

    def states(self) -> list:
        data = self._get("/api/states")
        if not isinstance(data, list):
            raise HaProtocolError("实体列表格式不对")
        return data

    def state(self, entity_id: str) -> dict:
        data = self._get("/api/states/" + entity_id)
        if not isinstance(data, dict):
            raise HaProtocolError("实体状态格式不对")
        return data

    def services(self) -> list:
        data = self._get("/api/services")
        if not isinstance(data, list):
            raise HaProtocolError("服务列表格式不对")
        return data

    # -- writes (batch 2 uses these; kept here so the client stays one unit)

    def call_service(self, domain: str, service: str,
                     data: dict | None = None) -> list:
        payload = dict(data or {})
        result = self._request("POST", "/api/services/%s/%s" % (domain, service),
                               payload)
        return result if isinstance(result, list) else []

    def process(self, text: str, language: str = "zh-CN",
                agent_id: str = "", conversation_id: str = "") -> dict:
        """POST /api/conversation/process: hand the sentence to HA's
        conversation agent. Pass a previous ``conversation_id`` to
        continue that session (multi-turn clarification); leave it out
        to start a fresh turn."""
        payload: dict[str, Any] = {"text": text}
        if language:
            payload["language"] = language
        if agent_id:
            payload["agent_id"] = agent_id
        if conversation_id:
            payload["conversation_id"] = conversation_id
        data = self._request("POST", "/api/conversation/process", payload)
        if not isinstance(data, dict):
            raise HaProtocolError("对话结果格式不对")
        return data
