"""Backwards-compatible reservation facade over the arbitration store.

The old scheduler kept one window per berth under ``berth.reservation.*``.
Legacy documents are migrated once into booking records; after that this
facade projects the live HELD bookings onto the old
:class:`~portpower.berth.schedule.Reservation` shape so existing endpoints
keep working.
"""

from __future__ import annotations

from portpower.berth.arbitration.booking import BookingState
from portpower.berth.arbitration.repository import ArbitrationRepository
from portpower.berth.schedule import Reservation, ReservationConflict
from portpower.store.kvstore import RecordStore

DEFAULT_GRACE_MINUTES = 15


class Scheduler:
    def __init__(self, store: RecordStore, repository: ArbitrationRepository) -> None:
        self._store = store
        self._repo = repository
        self._migrate_legacy()

    # --- migration ----------------------------------------------------------

    def _migrate_legacy(self) -> None:
        from portpower.store.query import keys_with_prefix

        legacy_prefix = "berth.reservation."
        for key in keys_with_prefix(self._store, legacy_prefix):
            legacy = Reservation.from_dict(self._store.get_json(key))
            if legacy is None:
                self._store.delete(key)
                continue
            if self._has_window(legacy.berth, legacy.start, legacy.end):
                self._store.delete(key)
                continue
            booking = self._build_legacy_booking(legacy)
            self._repo.bookings.save(booking)
            self._store.delete(key)

    def _build_legacy_booking(self, legacy: Reservation):
        from portpower.berth.arbitration.booking import Booking

        booking_id = self._repo.new_booking_id()
        return Booking(
            booking_id=booking_id,
            berth=legacy.berth,
            ship=legacy.ship,
            start=legacy.start,
            end=legacy.end,
            grace_minutes=DEFAULT_GRACE_MINUTES,
        )

    def _has_window(self, berth: str, start: str, end: str) -> bool:
        for booking in self._repo.bookings.for_berth(
            berth, frozenset({BookingState.HELD})
        ):
            if booking.start == start and booking.end == end:
                return True
        return False

    # --- legacy API ---------------------------------------------------------

    def book(self, reservation: Reservation) -> None:
        for held in self._repo.bookings.for_berth(
            reservation.berth, frozenset({BookingState.HELD})
        ):
            if held.overlaps(reservation.start, reservation.end):
                raise ReservationConflict(
                    f"berth {reservation.berth} busy {held.start}..{held.end}"
                )
        from portpower.berth.arbitration.booking import Booking

        self._repo.bookings.save(self._build_legacy_booking(reservation))

    def get(self, berth: str) -> Reservation | None:
        held = [
            booking
            for booking in self._repo.bookings.for_berth(
                berth, frozenset({BookingState.HELD})
            )
        ]
        if not held:
            return None
        booking = min(held, key=lambda item: item.start)
        return Reservation(
            berth=booking.berth,
            ship=booking.ship,
            start=booking.start,
            end=booking.end,
        )

    def cancel(self, berth: str) -> None:
        for booking in self._repo.bookings.for_berth(
            berth, frozenset({BookingState.HELD})
        ):
            self._repo.bookings.save(booking.as_state(BookingState.CANCELLED))

    def list_all(self) -> list[Reservation]:
        return [
            Reservation(
                berth=booking.berth,
                ship=booking.ship,
                start=booking.start,
                end=booking.end,
            )
            for booking in self._repo.bookings.all()
            if booking.state is BookingState.HELD
        ]

    def count(self) -> int:
        return len(self.list_all())
