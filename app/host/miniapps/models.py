"""Value types and the wire schema for mini-app results and cards.

The device never sees a provider's raw response. It receives a validated
``card`` dict describing rings, text rows, colours and reset timestamps,
and renders those with a native Canvas view. Absolute timestamps let the
device own the countdown: one command is enough, no image stream.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SCHEMA_VERSION = 1

STATUSES = frozenset({"loading", "ok", "partial", "stale", "error"})

ERROR_CODES = frozenset({
    "not_logged_in", "unsupported_auth", "unsupported_method",
    "unsupported_window", "timeout", "upstream_error", "render_failed",
    "field_error", "unavailable",
})

# Device-side limits. The same numbers are enforced in MiniAppCardView.java.
MAX_RINGS = 2
MAX_ROWS = 6
MAX_TEXT_CHARS = 120
MAX_CARD_CHARS = 4000


def now_iso(ts: float | None = None) -> str:
    """Local ISO timestamp; the device only uses it for display."""
    moment = datetime.fromtimestamp(ts) if ts is not None else datetime.now()
    return moment.astimezone().isoformat(timespec="seconds")


@dataclass
class QueryResult:
    """One query's answer. The card and the spoken summary come from this."""
    app_id: str
    operation: str
    status: str = "ok"
    data: dict[str, Any] = field(default_factory=dict)
    speech: str = ""
    source: str = ""
    error_code: str | None = None
    fetched_at: str = ""
    data_through: str | None = None
    request_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "app_id": self.app_id,
            "request_id": self.request_id,
            "operation": self.operation,
            "status": self.status,
            "fetched_at": self.fetched_at,
            "data_through": self.data_through,
            "source": self.source,
            "error_code": self.error_code,
            "data": self.data,
            "speech": self.speech,
        }

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        if not self.app_id or not self.operation:
            raise ValueError("app_id and operation are required")
        if self.status not in STATUSES:
            raise ValueError("unknown status: %s" % self.status)
        if self.error_code is not None and self.error_code not in ERROR_CODES:
            raise ValueError("unknown error_code: %s" % self.error_code)
        if not isinstance(self.data, dict):
            raise ValueError("data must be an object")
        if not isinstance(self.speech, str) or len(self.speech) > 100:
            raise ValueError("speech must be a short string")


def _check_text(value: Any, where: str) -> None:
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % where)
    if len(value) > MAX_TEXT_CHARS:
        raise ValueError("%s is too long" % where)


def _check_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a finite number" % where)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("%s must be a finite number" % where)
    return number


def validate_card(card: dict[str, Any]) -> None:
    """Reject anything the device renderer cannot draw safely.

    Raw upstream values are never trusted: negatives, NaN, strings that look
    like numbers and out-of-range percentages must fail here, not turn into
    a plausible-looking zero on the screen.
    """
    if not isinstance(card, dict):
        raise ValueError("card must be an object")
    template = card.get("template")
    if template not in ("rings", "lines"):
        raise ValueError("unknown card template")
    layout = card.get("layout")
    if layout is not None and (template != "lines" or layout not in
                               ("home_light", "home_climate", "home_feedback", "bambu_print")):
        raise ValueError("unknown lines layout")
    if layout == "bambu_print":
        fields = card.get("printer")
        if not isinstance(fields, dict):
            raise ValueError("printer fields must be an object")
        progress = fields.get("progress")
        if progress is not None and not 0 <= _check_number(progress, "printer progress") <= 100:
            raise ValueError("printer progress must be between 0 and 100")
        for key in ("progress_text", "state", "nozzle", "bed", "layers", "task", "remaining"):
            _check_text(fields.get(key), "printer " + key)
    track = card.get("track_color")
    if track is not None:
        _check_text(track, "track_color")
        if not (isinstance(track, str) and len(track) == 7
                and track.startswith("#")):
            raise ValueError("track_color must be #rrggbb")
    _check_text(card.get("title", ""), "title")
    _check_text(card.get("footer", ""), "footer")
    _check_text(card.get("notice", ""), "notice")
    if "icon" in card:
        _check_text(card["icon"], "icon")
        if not card["icon"].endswith(".png") or "/" in card["icon"]:
            raise ValueError("icon must be a bundled asset name")
    if card.get("status") not in STATUSES:
        raise ValueError("unknown card status")

    rows = card.get("rows", [])
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError("rows must be a short array")
    if layout == "home_feedback" and not 1 <= len(rows) <= 2:
        raise ValueError("home feedback needs one or two pages")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("row %d must be an object" % index)
        _check_text(row.get("label", ""), "row label")
        _check_text(row.get("value", ""), "row value")
        if layout == "home_feedback":
            icon = row.get("icon", "")
            _check_text(icon, "home icon")
            if not icon.startswith("ha-") or not icon.endswith(".png") or "/" in icon:
                raise ValueError("home icon must be a bundled HA asset name")
            if row.get("unit", "") not in ("", "%", "°C", "°F"):
                raise ValueError("unknown home feedback unit")
            if row.get("tone", "normal") not in ("normal", "off"):
                raise ValueError("unknown home feedback tone")
            if row.get("swatch", "") not in ("", "warm", "cool"):
                raise ValueError("unknown home feedback swatch")
            if "level" in row and not 0 <= _check_number(row["level"], "home level") <= 100:
                raise ValueError("home level must be between 0 and 100")

    rings = card.get("rings", [])
    if not isinstance(rings, list) or len(rings) > MAX_RINGS:
        raise ValueError("rings must be a short array")
    for index, ring in enumerate(rings):
        if not isinstance(ring, dict):
            raise ValueError("ring %d must be an object" % index)
        _check_text(ring.get("label", ""), "ring label")
        value = ring.get("value")
        if value is None:
            continue  # upstream field anomaly: the device shows a dash
        number = _check_number(value, "ring value")
        if number < 0:
            raise ValueError("ring value must not be negative")
        colour = ring.get("color", "")
        _check_text(colour, "ring color")
        if not (isinstance(colour, str) and len(colour) == 7
                and colour.startswith("#")):
            raise ValueError("ring color must be #rrggbb")
        for key in ("radius", "width"):
            size = _check_number(ring.get(key, 0), "ring " + key)
            if size <= 0 or size > 320:
                raise ValueError("ring %s out of range" % key)
        reset = ring.get("reset_at_ms")
        if reset is not None:
            _check_number(reset, "reset_at_ms")

    if template == "lines" and rings:
        raise ValueError("lines cards cannot contain rings")
    if template == "rings" and not rings:
        raise ValueError("rings cards need at least one ring")
    if len(str(card)) > MAX_CARD_CHARS * 4:
        raise ValueError("card payload too large")
