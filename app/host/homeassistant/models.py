"""Value types for the Home Assistant integration.

The device never sees HA's raw response. Control turns come back as a
``HaResult`` whose status is deliberately pessimistic: HTTP 200 and a
polite "done" from HA are not proof that a device moved, so results are
``reported_success`` (HA said so) until an optional state read confirms
them, and a timeout is ``unknown`` -- the request may well have run.

Card projection maps onto the mini-app card vocabulary
(loading/ok/partial/stale/error); ``unknown`` must never render as a
green success row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

# Internal state machine (not the card vocabulary).
STATUSES = ("queued", "running", "reported_success", "partial",
            "failed", "unknown")
VERIFICATIONS = ("not_checked", "state_confirmed", "state_mismatch")
KINDS = ("control", "query", "overview")

# How each HaResult status shows on a card. "unknown" is a stale/unconfirmed
# state, not a success -- the row says so explicitly (cards.py, batch 2).
CARD_STATUS = {
    "queued": "loading",
    "running": "loading",
    "reported_success": "ok",
    "partial": "partial",
    "failed": "error",
    "unknown": "stale",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalise_base_url(value: str) -> str:
    """Validate and tidy a Home Assistant base URL.

    http/https only, no embedded credentials, trailing slash removed.
    A URL pointing at anything but the HA root would silently 404 later,
    so the shape is checked once at save time.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ValueError("地址必须以 http:// 或 https:// 开头")
    if not parts.netloc:
        raise ValueError("地址缺少主机名")
    if parts.username or parts.password:
        raise ValueError("地址中不要写用户名密码，请用访问令牌")
    if parts.query or parts.fragment:
        raise ValueError("地址不要带查询参数或锚点")
    path = parts.path.rstrip("/")
    if path and path != "/api":
        # A pasted /api/states URL is a common mistake.
        raise ValueError("地址写到主机名为止，例如 http://192.0.2.4:8123")
    return f"{parts.scheme}://{parts.netloc}"


@dataclass
class HaResult:
    """One HA turn's outcome. Kept out of QueryResult on purpose: control
    commands must never share the mini-app query cache."""
    request_id: str = ""
    kind: str = "control"
    status: str = "unknown"
    message: str = ""
    targets: list[dict[str, Any]] = field(default_factory=list)
    verification: str = "not_checked"
    conversation_id: str = ""
    received_at: str = ""

    def __post_init__(self) -> None:
        if not self.received_at:
            self.received_at = now_iso()

    @property
    def card_status(self) -> str:
        status = CARD_STATUS.get(self.status, "stale")
        # A polite "done" is not proof a device moved. Only a confirmed
        # state read may show green; a mismatch is stale, never ok.
        if status == "ok" and self.verification == "state_mismatch":
            return "stale"
        return status

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "kind": self.kind,
            "status": self.status,
            "card_status": self.card_status,
            "message": self.message,
            "targets": list(self.targets),
            "verification": self.verification,
            "conversation_id": self.conversation_id,
            "received_at": self.received_at,
        }
