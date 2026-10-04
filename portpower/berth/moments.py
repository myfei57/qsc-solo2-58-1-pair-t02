"""ISO-8601 moment helpers shared by the berth arbitration rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def parse_moment(text: str) -> datetime:
    """Parse an ISO-8601 moment, treating naive values as UTC."""

    raw = text.strip()
    if not raw:
        raise ValueError("moment is required")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid moment {text!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def format_moment(moment: datetime) -> str:
    """Render ``moment`` in the ledger's canonical UTC shape."""

    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def normalize_moment(text: str) -> str:
    return format_moment(parse_moment(text))


def shift_moment(text: str, minutes: float) -> str:
    return format_moment(parse_moment(text) + timedelta(minutes=minutes))


def validate_window(start: str, end: str) -> tuple[str, str]:
    """Normalize both ends of a window, rejecting inverted ranges."""

    opening = normalize_moment(start)
    closing = normalize_moment(end)
    if opening >= closing:
        raise ValueError("window is inverted")
    return opening, closing


def utcnow_text() -> str:
    return format_moment(datetime.now(timezone.utc))
