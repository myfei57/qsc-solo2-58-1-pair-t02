"""Reservation record with its arbitration lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class BookingState(str, Enum):
    HELD = "held"          # window reserved, ship has not arrived yet
    HONORED = "honored"    # ship arrived inside the window and occupies the berth
    BUMPED = "bumped"      # pre-empted by a walk-in (R22); eligible for rebook
    EXPIRED = "expired"    # no-show after grace (R30); eligible for rebook
    CANCELLED = "cancelled"  # explicitly cancelled; terminal


# States from which the reservation may be arranged onto a new window.
BOOKING_REBOOKABLE: frozenset[BookingState] = frozenset(
    {BookingState.BUMPED, BookingState.EXPIRED}
)

_TERMINAL: frozenset[BookingState] = frozenset({BookingState.CANCELLED})


@dataclass(frozen=True)
class Booking:
    """One reservation. Timestamps are ISO-8601 strings, half-open window."""

    booking_id: str
    berth: str
    ship: str
    start: str
    end: str
    grace_minutes: int = 15
    state: BookingState = BookingState.HELD
    version: int = 1  # bumped/rebooked chains share the id, version advances

    def __post_init__(self) -> None:
        if not self.booking_id.strip():
            raise ValueError("booking id is required")
        if not self.berth.strip():
            raise ValueError("booking berth is required")
        if not self.ship.strip():
            raise ValueError("booking ship is required")
        if self.start >= self.end:
            raise ValueError("booking window is inverted")
        if self.grace_minutes < 0:
            raise ValueError("grace minutes must be non-negative")

    # --- predicates ---------------------------------------------------------

    @property
    def rebookable(self) -> bool:
        return self.state in BOOKING_REBOOKABLE

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL

    def overlaps(self, start: str, end: str) -> bool:
        return self.start < end and start < self.end

    def active_at(self, moment: str) -> bool:
        """True while the window itself is in effect: ``start <= now < end``."""

        return self.start <= moment < self.end

    def grace_deadline(self) -> str:
        from datetime import timedelta, timezone

        from portpower.berth.arbitration.clock import parse

        start = parse(self.start)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        deadline = start + timedelta(minutes=self.grace_minutes)
        return deadline.replace(microsecond=0).isoformat()

    def in_grace_at(self, moment: str) -> bool:
        return self.start <= moment < self.grace_deadline()

    def lapsed_at(self, moment: str) -> bool:
        """Past start plus grace with no honouring arrival."""

        return self.state is BookingState.HELD and moment >= self.grace_deadline()

    # --- transitions --------------------------------------------------------

    def as_state(self, state: BookingState) -> "Booking":
        return replace(self, state=state)

    def rebook(self, berth: str, start: str, end: str) -> "Booking":
        if self.terminal:
            raise ValueError(f"booking {self.booking_id} is {self.state.value}")
        if start >= end:
            raise ValueError("booking window is inverted")
        return replace(
            self,
            berth=berth,
            start=start,
            end=end,
            state=BookingState.HELD,
            version=self.version + 1,
        )

    # --- (de)serialisation --------------------------------------------------

    def as_dict(self) -> dict[str, object]:
        return {
            "booking_id": self.booking_id,
            "berth": self.berth,
            "ship": self.ship,
            "start": self.start,
            "end": self.end,
            "grace_minutes": self.grace_minutes,
            "state": self.state.value,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "Booking | None":
        if not isinstance(payload, dict):
            return None
        try:
            state_raw = payload.get("state", BookingState.HELD.value)
            state = BookingState(str(state_raw))
            return cls(
                booking_id=str(payload["booking_id"]),
                berth=str(payload["berth"]),
                ship=str(payload["ship"]),
                start=str(payload["start"]),
                end=str(payload["end"]),
                grace_minutes=int(payload.get("grace_minutes", 15)),
                state=state,
                version=int(payload.get("version", 1)),
            )
        except (KeyError, ValueError, TypeError):
            return None
