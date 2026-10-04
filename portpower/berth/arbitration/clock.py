"""Time source so arbitration rules can be tested against a fixed clock."""

from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    def now(self) -> str:
        raise NotImplementedError


class SystemClock(Clock):
    """Wall-clock UTC timestamps rounded to whole seconds (ISO-8601)."""

    def now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class FixedClock(Clock):
    """Deterministic clock used by the arbitration tests."""

    def __init__(self, moment: str) -> None:
        self._moment = moment

    def set(self, moment: str) -> None:
        self._moment = moment

    def now(self) -> str:
        return self._moment


def parse(moment: str) -> datetime:
    """Parse the ISO-8601 moments used across the berth domain."""

    text = moment.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def is_after(winner: str, other: str) -> bool:
    return parse(winner) > parse(other)
