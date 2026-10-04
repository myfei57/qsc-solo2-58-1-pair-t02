"""One arbitration path for reservations, walk-ins, timeouts and queues.

Every berth ruling — booking, walk-in placement, preemption, no-show
expiry, queue promotion — flows through :class:`Arbiter` so that each
outcome is recorded with the rule that produced it and the moment it
took effect.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from uuid import uuid4

from portpower.audit.ledger import Ledger
from portpower.berth import rules
from portpower.berth.decisions import Decision, DecisionLog, Outcome
from portpower.berth.moments import (
    normalize_moment,
    shift_moment,
    utcnow_text,
    validate_window,
)
from portpower.berth.queue import QueuedRequest, WaitQueue
from portpower.berth.registry import BerthRegistry
from portpower.berth.schedule import (
    REBOOKABLE_STATUSES,
    Reservation,
    ReservationConflict,
    ReservationStatus,
    Scheduler,
)
from portpower.ns.catalog import Catalog
from portpower.ship.fleet import Fleet

DEFAULT_GRACE_MINUTES = 30
DEFAULT_STAY_MINUTES = 240


def _ticket_code(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


class Arbiter:
    """Single decision point that reservations and walk-ins share."""

    def __init__(
        self,
        registry: BerthRegistry,
        catalog: Catalog,
        scheduler: Scheduler,
        queue: WaitQueue,
        decisions: DecisionLog,
        ledger: Ledger,
        fleet: Fleet,
        clock: Callable[[], str] = utcnow_text,
        grace_minutes: float = DEFAULT_GRACE_MINUTES,
        stay_minutes: float = DEFAULT_STAY_MINUTES,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._scheduler = scheduler
        self._queue = queue
        self._decisions = decisions
        self._ledger = ledger
        self._fleet = fleet
        self._clock = clock
        self._grace = grace_minutes
        self._stay = stay_minutes
        self._lock = threading.RLock()

    # ---- booking -------------------------------------------------

    def book(
        self,
        ship: str,
        berth: str,
        start: str,
        end: str,
        moment: str | None = None,
    ) -> Reservation:
        """Reserve ``berth`` for ``ship`` over ``start..end`` (rule R0)."""

        with self._lock:
            now = self._moment(moment)
            if not self._catalog.has(berth):
                raise LookupError(f"unknown berth {berth!r}")
            opening, closing = validate_window(start, end)
            self._ensure_window_free(berth, opening, closing, ignore=None)
            reservation = Reservation(
                code=_ticket_code("rsv"),
                berth=berth,
                ship=ship,
                start=opening,
                end=closing,
                status=ReservationStatus.BOOKED.value,
                created=now,
                updated=now,
            )
            self._scheduler.save(reservation)
            self._record(
                at=now,
                berth=berth,
                ship=ship,
                rule=rules.BOOKING_ACCEPTED,
                outcome=Outcome.BOOKED,
                effective_at=opening,
                booking=reservation.code,
                reason=f"window {opening}..{closing}",
            )
            return reservation

    def rebook(
        self,
        code: str,
        start: str,
        end: str,
        berth: str | None = None,
        moment: str | None = None,
    ) -> Reservation:
        """Give a displaced or expired reservation a new window (rule R5)."""

        with self._lock:
            now = self._moment(moment)
            reservation = self._reservation(code)
            if reservation.status not in REBOOKABLE_STATUSES:
                raise ReservationConflict(
                    f"reservation {code} is {reservation.status} and cannot be rebooked"
                )
            target = berth or reservation.berth
            if not self._catalog.has(target):
                raise LookupError(f"unknown berth {target!r}")
            opening, closing = validate_window(start, end)
            self._ensure_window_free(target, opening, closing, ignore=code)
            updated = reservation.rescheduled(
                target,
                opening,
                closing,
                now,
                f"rebooked after {reservation.status}",
            )
            self._scheduler.save(updated)
            self._record(
                at=now,
                berth=target,
                ship=updated.ship,
                rule=rules.REBOOK,
                outcome=Outcome.REBOOKED,
                effective_at=opening,
                booking=code,
                reason=f"rebooked after {reservation.status}; window {opening}..{closing}",
            )
            return updated

    def cancel(self, code: str, moment: str | None = None) -> Reservation:
        with self._lock:
            now = self._moment(moment)
            reservation = self._reservation(code)
            if reservation.status in (
                ReservationStatus.FULFILLED.value,
                ReservationStatus.CANCELLED.value,
            ):
                raise ReservationConflict(
                    f"reservation {code} is {reservation.status} and cannot be cancelled"
                )
            cancelled = reservation.with_status(
                ReservationStatus.CANCELLED, now, "cancelled by request"
            )
            self._scheduler.save(cancelled)
            self._record(
                at=now,
                berth=cancelled.berth,
                ship=cancelled.ship,
                rule=rules.RECORD_KEEPING,
                outcome=Outcome.CANCELLED,
                effective_at=now,
                booking=code,
                reason="cancelled by request",
            )
            return cancelled

    # ---- unified arrival -----------------------------------------

    def arrive(
        self,
        ship: str,
        berth: str | None = None,
        booking: str | None = None,
        stay_end: str | None = None,
        preempt: bool = False,
        reason: str = "",
        moment: str | None = None,
    ) -> list[Decision]:
        """Decide where an arriving ship goes.

        With ``booking`` the reservation lifecycle drives the ruling
        (rules R1/R4); without it the ship is a walk-in and either fits
        a gap (R2), explicitly preempts a reservation (R3), or waits in
        the queue. The last decision in the returned list is the
        headline outcome for the arriving ship.
        """

        with self._lock:
            now = self._moment(moment)
            if booking:
                reservation = self._reservation(booking)
                if reservation.ship != ship:
                    raise ReservationConflict(
                        f"reservation {booking} belongs to {reservation.ship}"
                    )
                if reservation.status != ReservationStatus.BOOKED.value:
                    raise ReservationConflict(
                        f"reservation {booking} is {reservation.status}; "
                        "rebook before arrival"
                    )
                deadline = shift_moment(reservation.start, self._grace)
                if now < reservation.start:
                    return [
                        self._enqueue(
                            ship=ship,
                            berth=reservation.berth,
                            start=now,
                            end=reservation.end,
                            now=now,
                            rule=rules.RESERVED_WINDOW,
                            booking=reservation.code,
                            note=f"window opens {reservation.start}",
                        )
                    ]
                if now <= deadline:
                    return self._honor(reservation, now)
                decisions = self._expire(reservation, now)
                berth = berth or reservation.berth
            else:
                decisions = []
            end = self._stay_end(stay_end, now)
            decisions.extend(
                self._place_walk_in(ship, berth, now, end, preempt, reason, now)
            )
            return decisions

    # ---- departure and timeout -----------------------------------

    def vacate(self, code: str, moment: str | None = None) -> list[Decision]:
        """Release ``code`` and promote the queue if someone fits (R6)."""

        with self._lock:
            now = self._moment(moment)
            occupant = self._registry.ship_at(code)
            if occupant is None:
                return []
            self._registry.release(code)
            self._fleet.undock(occupant)
            decisions: list[Decision] = []
            for reservation in self._scheduler.for_ship(occupant):
                if (
                    reservation.berth == code
                    and reservation.status == ReservationStatus.CHECKED_IN.value
                ):
                    self._scheduler.save(
                        reservation.with_status(
                            ReservationStatus.FULFILLED, now, "stay completed"
                        )
                    )
                    decisions.append(
                        self._record(
                            at=now,
                            berth=code,
                            ship=occupant,
                            rule=rules.RESERVED_WINDOW,
                            outcome=Outcome.FULFILLED,
                            effective_at=now,
                            booking=reservation.code,
                            reason="reserved stay completed",
                        )
                    )
            decisions.append(
                self._record(
                    at=now,
                    berth=code,
                    ship=occupant,
                    rule=rules.RECORD_KEEPING,
                    outcome=Outcome.RELEASED,
                    effective_at=now,
                    reason="berth vacated",
                )
            )
            decisions.extend(self._promote(now))
            return decisions

    def sweep(self, moment: str | None = None) -> list[Decision]:
        """Expire no-show reservations past their grace deadline (R4)."""

        with self._lock:
            now = self._moment(moment)
            decisions: list[Decision] = []
            booked = self._scheduler.list_all(status=ReservationStatus.BOOKED.value)
            for reservation in booked:
                deadline = shift_moment(reservation.start, self._grace)
                if deadline < now:
                    decisions.extend(self._expire(reservation, now))
                    decisions.extend(self._promote(now))
            return decisions

    # ---- read models ----------------------------------------------

    def decisions(
        self,
        berth: str | None = None,
        ship: str | None = None,
        rule: str | None = None,
        booking: str | None = None,
    ) -> list[Decision]:
        entries = self._decisions.all()
        if berth:
            entries = [entry for entry in entries if entry.berth == berth]
        if ship:
            entries = [entry for entry in entries if entry.ship == ship]
        if rule:
            entries = [entry for entry in entries if entry.rule == rule]
        if booking:
            entries = [entry for entry in entries if entry.booking == booking]
        return entries

    def queue(self) -> list[QueuedRequest]:
        return self._queue.entries()

    def queue_position(self, ticket: str) -> int | None:
        return self._queue.position(ticket)

    # ---- internals -------------------------------------------------

    def _moment(self, moment: str | None) -> str:
        return normalize_moment(moment) if moment else self._clock()

    def _stay_end(self, stay_end: str | None, now: str) -> str:
        end = normalize_moment(stay_end) if stay_end else shift_moment(now, self._stay)
        if end <= now:
            raise ValueError("stay_end must be later than now")
        return end

    def _reservation(self, code: str) -> Reservation:
        reservation = self._scheduler.get(code)
        if reservation is None:
            raise LookupError(f"unknown reservation {code!r}")
        return reservation

    def _honor(self, reservation: Reservation, now: str) -> list[Decision]:
        berth = self._registry.get(reservation.berth)
        if berth is not None and not berth.occupied:
            self._assign(reservation.berth, reservation.ship)
            self._scheduler.save(
                reservation.with_status(
                    ReservationStatus.CHECKED_IN, now, "checked in within window"
                )
            )
            return [
                self._record(
                    at=now,
                    berth=reservation.berth,
                    ship=reservation.ship,
                    rule=rules.RESERVED_WINDOW,
                    outcome=Outcome.ASSIGNED,
                    effective_at=now,
                    booking=reservation.code,
                    reason="reserved window honored",
                )
            ]
        occupant = self._registry.ship_at(reservation.berth)
        return [
            self._enqueue(
                ship=reservation.ship,
                berth=reservation.berth,
                start=now,
                end=reservation.end,
                now=now,
                rule=rules.RESERVED_WINDOW,
                booking=reservation.code,
                note=f"berth occupied by {occupant or 'unknown'}",
            )
        ]

    def _place_walk_in(
        self,
        ship: str,
        berth: str | None,
        start: str,
        end: str,
        preempt: bool,
        reason: str,
        now: str,
    ) -> list[Decision]:
        candidates = self._candidates(berth)
        for code in candidates:
            if self._fits(code, start, end):
                self._assign(code, ship)
                return [
                    self._record(
                        at=now,
                        berth=code,
                        ship=ship,
                        rule=rules.WALK_IN_GAP,
                        outcome=Outcome.ASSIGNED,
                        effective_at=start,
                        reason=f"fits gap until {self._next_booking_start(code)}",
                    )
                ]
        if preempt:
            if not reason.strip():
                raise ValueError("preemption requires a reason")
            for code in candidates:
                berth_obj = self._registry.get(code)
                if berth_obj is None or berth_obj.occupied:
                    continue
                blocking = self._blocking(code, start, end)
                if blocking is None:
                    continue
                return self._preempt(code, ship, blocking, start, reason, now)
        return [
            self._enqueue(
                ship=ship,
                berth=berth or "",
                start=start,
                end=end,
                now=now,
                rule=rules.WALK_IN_GAP,
                booking=None,
                note="no berth with a free window",
            )
        ]

    def _preempt(
        self,
        code: str,
        ship: str,
        blocking: Reservation,
        start: str,
        reason: str,
        now: str,
    ) -> list[Decision]:
        self._scheduler.save(
            blocking.with_status(
                ReservationStatus.PREEMPTED, now, f"preempted by {ship}: {reason}"
            )
        )
        decisions = [
            self._record(
                at=now,
                berth=code,
                ship=blocking.ship,
                rule=rules.EXPLICIT_PREEMPTION,
                outcome=Outcome.PREEMPTED,
                effective_at=now,
                booking=blocking.code,
                reason=reason,
            )
        ]
        self._assign(code, ship)
        decisions.append(
            self._record(
                at=now,
                berth=code,
                ship=ship,
                rule=rules.EXPLICIT_PREEMPTION,
                outcome=Outcome.ASSIGNED,
                effective_at=start,
                displaced=blocking.code,
                reason=reason,
            )
        )
        return decisions

    def _expire(self, reservation: Reservation, now: str) -> list[Decision]:
        deadline = shift_moment(reservation.start, self._grace)
        self._scheduler.save(
            reservation.with_status(
                ReservationStatus.EXPIRED, now, f"no check-in by {deadline}"
            )
        )
        return [
            self._record(
                at=now,
                berth=reservation.berth,
                ship=reservation.ship,
                rule=rules.NO_SHOW_TIMEOUT,
                outcome=Outcome.EXPIRED,
                effective_at=deadline,
                booking=reservation.code,
                reason=f"no check-in by {deadline}; berth yielded",
            )
        ]

    def _promote(self, now: str) -> list[Decision]:
        decisions: list[Decision] = []
        while True:
            entries = self._queue.entries()
            stale = next((entry for entry in entries if entry.end <= now), None)
            if stale is not None:
                self._queue.remove(stale.ticket)
                decisions.append(
                    self._record(
                        at=now,
                        berth=stale.berth,
                        ship=stale.ship,
                        rule=rules.RECORD_KEEPING,
                        outcome=Outcome.DROPPED,
                        effective_at=now,
                        booking=stale.booking,
                        reason="queued window lapsed",
                    )
                )
                continue
            placed: tuple[QueuedRequest, str] | None = None
            for entry in entries:
                target = self._first_fit(entry, now)
                if target is not None:
                    placed = (entry, target)
                    break
            if placed is None:
                return decisions
            entry, target = placed
            self._queue.remove(entry.ticket)
            self._assign(target, entry.ship)
            if entry.booking:
                reservation = self._scheduler.get(entry.booking)
                if reservation is not None and reservation.booked:
                    self._scheduler.save(
                        reservation.with_status(
                            ReservationStatus.CHECKED_IN, now, "promoted from queue"
                        )
                    )
            decisions.append(
                self._record(
                    at=now,
                    berth=target,
                    ship=entry.ship,
                    rule=rules.QUEUE_PROMOTION,
                    outcome=Outcome.PROMOTED,
                    effective_at=now,
                    booking=entry.booking,
                    reason="promoted from queue",
                )
            )

    def _enqueue(
        self,
        ship: str,
        berth: str,
        start: str,
        end: str,
        now: str,
        rule: rules.Rule,
        booking: str | None,
        note: str,
    ) -> Decision:
        entry = QueuedRequest(
            ticket=_ticket_code("q"),
            ship=ship,
            berth=berth,
            start=start,
            end=end,
            enqueued_at=now,
            booking=booking,
            note=note,
        )
        self._queue.enqueue(entry)
        position = self._queue.position(entry.ticket)
        return self._record(
            at=now,
            berth=berth,
            ship=ship,
            rule=rule,
            outcome=Outcome.QUEUED,
            effective_at=start,
            booking=booking,
            ticket=entry.ticket,
            reason=f"queued at position {position}: {note}",
        )

    def _assign(self, code: str, ship: str) -> None:
        berth = self._registry.get(code)
        if berth is None:
            raise LookupError(f"unknown berth {code!r}")
        berth.assign(ship)
        self._fleet.add(ship).moor(code)

    def _candidates(self, berth: str | None) -> list[str]:
        if berth is None:
            return self._catalog.codes()
        if not self._catalog.has(berth):
            raise LookupError(f"unknown berth {berth!r}")
        return [berth]

    def _first_fit(self, entry: QueuedRequest, now: str) -> str | None:
        candidates = [entry.berth] if entry.berth else self._catalog.codes()
        for code in candidates:
            if self._fits(code, now, entry.end):
                return code
        return None

    def _fits(self, code: str, start: str, end: str) -> bool:
        berth = self._registry.get(code)
        if berth is None or berth.occupied:
            return False
        return self._blocking(code, start, end) is None

    def _blocking(self, code: str, start: str, end: str) -> Reservation | None:
        blocking = [
            item
            for item in self._scheduler.booked_on(code)
            if item.overlaps_window(start, end)
        ]
        if not blocking:
            return None
        blocking.sort(key=lambda item: (item.start, item.code))
        return blocking[0]

    def _ensure_window_free(
        self, berth: str, start: str, end: str, ignore: str | None
    ) -> None:
        for item in self._scheduler.booked_on(berth):
            if ignore is not None and item.code == ignore:
                continue
            if item.overlaps_window(start, end):
                raise ReservationConflict(
                    f"berth {berth} busy {item.start}..{item.end} ({item.code})"
                )

    def _next_booking_start(self, code: str) -> str:
        upcoming = self._scheduler.booked_on(code)
        return upcoming[0].start if upcoming else "none"

    def _record(
        self,
        *,
        at: str,
        berth: str,
        ship: str,
        rule: rules.Rule,
        outcome: Outcome,
        effective_at: str,
        reason: str = "",
        booking: str | None = None,
        displaced: str | None = None,
        ticket: str | None = None,
    ) -> Decision:
        decision = self._decisions.record(
            at=at,
            berth=berth,
            ship=ship,
            rule=rule.code,
            outcome=outcome.value,
            effective_at=effective_at,
            reason=reason,
            booking=booking,
            displaced=displaced,
            ticket=ticket,
        )
        detail = f"{berth} {ship} rule={rule.code} effective={effective_at}"
        if booking:
            detail += f" booking={booking}"
        if displaced:
            detail += f" displaced={displaced}"
        if reason:
            detail += f" {reason}"
        self._ledger.append(f"berth.{outcome.value}", detail)
        return decision
