"""Berth reservations with a lifecycle that survives preemption and expiry."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum

from portpower.store.kvstore import RecordStore

PREFIX = "berth.reservation."


def reservation_key(code: str) -> str:
    return PREFIX + code


class ReservationStatus(str, Enum):
    BOOKED = "booked"
    CHECKED_IN = "checked_in"
    FULFILLED = "fulfilled"
    PREEMPTED = "preempted"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


REBOOKABLE_STATUSES = frozenset(
    {
        ReservationStatus.BOOKED.value,
        ReservationStatus.PREEMPTED.value,
        ReservationStatus.EXPIRED.value,
    }
)

TERMINAL_STATUSES = frozenset(
    {
        ReservationStatus.FULFILLED.value,
        ReservationStatus.CANCELLED.value,
    }
)


@dataclass(frozen=True)
class Reservation:
    """One booking. Preempted and expired bookings stay on record."""

    code: str
    berth: str
    ship: str
    start: str
    end: str
    status: str = ReservationStatus.BOOKED.value
    created: str = ""
    updated: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("reservation code is required")
        if not self.berth.strip():
            raise ValueError("reservation berth is required")
        if not self.ship.strip():
            raise ValueError("reservation ship is required")
        if self.start >= self.end:
            raise ValueError("reservation window is inverted")

    @property
    def booked(self) -> bool:
        return self.status == ReservationStatus.BOOKED.value

    @property
    def rebookable(self) -> bool:
        return self.status in REBOOKABLE_STATUSES

    def overlaps_window(self, start: str, end: str) -> bool:
        return self.start < end and start < self.end

    def with_status(
        self, status: ReservationStatus, moment: str, note: str
    ) -> "Reservation":
        return replace(self, status=status.value, updated=moment, note=note)

    def rescheduled(
        self, berth: str, start: str, end: str, moment: str, note: str
    ) -> "Reservation":
        return replace(
            self,
            berth=berth,
            start=start,
            end=end,
            status=ReservationStatus.BOOKED.value,
            updated=moment,
            note=note,
        )

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> "Reservation | None":
        if not isinstance(payload, dict):
            return None
        try:
            status = str(payload.get("status") or ReservationStatus.BOOKED.value)
            ReservationStatus(status)
            return cls(
                code=str(payload["code"]),
                berth=str(payload["berth"]),
                ship=str(payload["ship"]),
                start=str(payload["start"]),
                end=str(payload["end"]),
                status=status,
                created=str(payload.get("created") or ""),
                updated=str(payload.get("updated") or ""),
                note=str(payload.get("note") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


class ReservationConflict(RuntimeError):
    """Raised when a booking overlaps an existing window on the same berth."""


class Scheduler:
    """Durable book of reservations, keyed by reservation code."""

    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def save(self, reservation: Reservation) -> None:
        self._store.put_json(reservation_key(reservation.code), reservation.as_dict())

    def get(self, code: str) -> Reservation | None:
        return Reservation.from_dict(self._store.get_json(reservation_key(code)))

    def for_berth(self, berth: str) -> list[Reservation]:
        return [item for item in self.list_all() if item.berth == berth]

    def booked_on(self, berth: str) -> list[Reservation]:
        return [item for item in self.for_berth(berth) if item.booked]

    def for_ship(self, ship: str) -> list[Reservation]:
        return [item for item in self.list_all() if item.ship == ship]

    def list_all(self, status: str | None = None) -> list[Reservation]:
        from portpower.store.query import keys_with_prefix

        items: list[Reservation] = []
        for key in keys_with_prefix(self._store, PREFIX):
            reservation = self.get(key[len(PREFIX) :])
            if reservation is None:
                continue
            if status is not None and reservation.status != status:
                continue
            items.append(reservation)
        items.sort(key=lambda item: (item.berth, item.start, item.code))
        return items

    def count(self) -> int:
        return len(self.list_all())
