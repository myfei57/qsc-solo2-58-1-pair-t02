"""Durable storage for bookings and the walk-in wait queue."""

from __future__ import annotations

from portpower.berth.arbitration.booking import Booking, BookingState
from portpower.berth.arbitration.waiter import Waiter, WaiterState
from portpower.store.kvstore import RecordStore
from portpower.store.query import keys_with_prefix

BOOKING_PREFIX = "berth.booking."
QUEUE_KEY = "berth.waitqueue"
SEQUENCE_KEY = "berth.sequence"


def booking_key(booking_id: str) -> str:
    return BOOKING_PREFIX + booking_id


class Sequence:
    """Monotonic counters used for booking, waiter and idempotency ids."""

    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def next(self, name: str) -> int:
        def mutate(document: object) -> tuple[object, int]:
            counters = document if isinstance(document, dict) else {}
            value = int(counters.get(name, 0)) + 1
            counters[name] = value
            return counters, value

        return self._store.update_json(SEQUENCE_KEY, mutate)


class BookingRepository:
    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def save(self, booking: Booking) -> None:
        self._store.put_json(booking_key(booking.booking_id), booking.as_dict())

    def get(self, booking_id: str) -> Booking | None:
        return Booking.from_dict(self._store.get_json(booking_key(booking_id)))

    def delete(self, booking_id: str) -> None:
        self._store.delete(booking_key(booking_id))

    def all(self) -> list[Booking]:
        items: list[Booking] = []
        for key in keys_with_prefix(self._store, BOOKING_PREFIX):
            booking = self.get(key[len(BOOKING_PREFIX) :])
            if booking is not None:
                items.append(booking)
        items.sort(key=lambda item: (item.start, item.booking_id))
        return items

    def for_berth(self, berth: str, states: frozenset[BookingState] | None = None) -> list[Booking]:
        return [
            booking
            for booking in self.all()
            if booking.berth == berth and (states is None or booking.state in states)
        ]

    def for_ship(self, ship: str) -> list[Booking]:
        return [booking for booking in self.all() if booking.ship == ship]


class WaitQueue:
    """FIFO queue; persisted as one document keyed by waiter id."""

    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def enqueue(self, waiter: Waiter) -> None:
        def mutate(document: object) -> tuple[object, None]:
            items = self._normalize(document)
            items = [item for item in items if item.waiter_id != waiter.waiter_id]
            items.append(waiter)
            self._order(items)
            return [item.as_dict() for item in items], None

        self._store.update_json(QUEUE_KEY, mutate)

    def update(self, waiter: Waiter) -> None:
        self.enqueue(waiter)

    def all(self) -> list[Waiter]:
        return self._order(self._normalize(self._store.get_json(QUEUE_KEY)))

    def waiting(self) -> list[Waiter]:
        return [item for item in self.all() if item.state is WaiterState.WAITING]

    def get(self, waiter_id: str) -> Waiter | None:
        for item in self.all():
            if item.waiter_id == waiter_id:
                return item
        return None

    def drop(self, waiter_id: str) -> bool:
        found = {"value": False}

        def mutate(document: object) -> tuple[object, None]:
            items = self._normalize(document)
            kept = [item for item in items if item.waiter_id != waiter_id]
            found["value"] = len(kept) != len(items)
            if not found["value"]:
                return None, None
            self._order(kept)
            return [item.as_dict() for item in kept], None

        self._store.update_json(QUEUE_KEY, mutate)
        return found["value"]

    @staticmethod
    def _normalize(document: object) -> list[Waiter]:
        if not isinstance(document, list):
            return []
        return [
            waiter
            for item in document
            if (waiter := Waiter.from_dict(item)) is not None
        ]

    @staticmethod
    def _order(items: list[Waiter]) -> list[Waiter]:
        items.sort(key=lambda item: (item.requested_at, item.waiter_id))
        return items


class ArbitrationRepository:
    """Facade grouping every persistent structure the arbiter touches."""

    def __init__(self, store: RecordStore) -> None:
        self.store = store
        self.sequences = Sequence(store)
        self.bookings = BookingRepository(store)
        self.queue = WaitQueue(store)

    def new_booking_id(self) -> str:
        return f"BK{self.sequences.next('booking'):06d}"

    def new_waiter_id(self) -> str:
        return f"WQ{self.sequences.next('waiter'):06d}"
