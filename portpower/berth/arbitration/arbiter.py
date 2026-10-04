"""The single arbiter that rules on both reservations and walk-ins.

Every state-changing berth decision goes through this class. Each public
method returns a :class:`Decision` naming the rule (``RuleId``) and the exact
``effective_at`` instant, appends an audit line, and records any party-level
result (bump / no-show / promotion) in the notification outbox.
"""

from __future__ import annotations

from portpower.audit.ledger import Ledger
from portpower.berth.arbitration.booking import Booking, BookingState
from portpower.berth.arbitration.clock import Clock
from portpower.berth.arbitration.decision import Decision, Outcome
from portpower.berth.arbitration.notice import Notice, Outbox
from portpower.berth.arbitration.occupancy import Occupancy
from portpower.berth.arbitration.repository import ArbitrationRepository
from portpower.berth.arbitration.rules import RuleId
from portpower.berth.arbitration.waiter import Waiter, WaiterState
from portpower.ns.catalog import Catalog


class BookingWindowConflict(ValueError):
    """Raised when a new/rescheduled booking overlaps a held reservation."""


class BookingNotFound(LookupError):
    """Raised when a booking id is unknown or not usable."""


class ReservationNotReady(ValueError):
    """Raised when a reserved ship arrives outside its honourable window."""


# Fictional "end of time" used to size walk-ins with no declared departure.
OPEN_ENDED = "9999-12-31T23:59:59+00:00"


class Arbiter:
    def __init__(
        self,
        repository: ArbitrationRepository,
        occupancy: Occupancy,
        catalog: Catalog,
        outbox: Outbox,
        ledger: Ledger,
        clock: Clock,
    ) -> None:
        self._repo = repository
        self._occupancy = occupancy
        self._catalog = catalog
        self._outbox = outbox
        self._ledger = ledger
        self._clock = clock

    # ------------------------------------------------------------------ #
    # Booking management
    # ------------------------------------------------------------------ #

    def book(
        self,
        ship: str,
        berth: str,
        start: str,
        end: str,
        grace_minutes: int = 15,
    ) -> Decision:
        self._require_berth(berth)
        if start >= end:
            raise ValueError("booking window is inverted")
        conflict = self._window_conflict(berth, start, end)
        if conflict is not None:
            decision = self._record(
                action="booking",
                outcome=Outcome.DENIED,
                rule=RuleId.BOOKING_WINDOW_CONFLICT,
                berth=berth,
                ship=ship,
                related=(conflict.booking_id,),
                reason=f"与预约 {conflict.booking_id} ({conflict.start}..{conflict.end}) 重叠",
            )
            raise BookingWindowConflict(self._audit_text(decision))

        booking = Booking(
            booking_id=self._repo.new_booking_id(),
            berth=berth,
            ship=ship,
            start=start,
            end=end,
            grace_minutes=grace_minutes,
        )
        self._repo.bookings.save(booking)
        return self._record(
            action="booking",
            outcome=Outcome.ACCEPTED,
            rule=RuleId.BOOKING_ACCEPTED,
            berth=berth,
            ship=ship,
            booking_id=booking.booking_id,
            reason=f"窗口 {start}..{end}，宽限 {grace_minutes} 分钟",
        )

    def rebook(
        self,
        booking_id: str,
        berth: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> Decision:
        booking = self._require_booking(booking_id)
        if not booking.rebookable:
            raise BookingNotFound(
                f"booking {booking_id} is {booking.state.value}, not rebookable"
            )
        target_berth = berth or booking.berth
        target_start = start or booking.start
        target_end = end or booking.end
        self._require_berth(target_berth)
        if target_start >= target_end:
            raise ValueError("booking window is inverted")
        conflict = self._window_conflict(
            target_berth, target_start, target_end, ignore=booking_id
        )
        if conflict is not None:
            raise BookingWindowConflict(
                f"berth {target_berth} busy with booking {conflict.booking_id} "
                f"{conflict.start}..{conflict.end}"
            )

        rebooked = booking.rebook(target_berth, target_start, target_end)
        self._repo.bookings.save(rebooked)
        return self._record(
            action="rebook",
            outcome=Outcome.REBOOKED,
            rule=RuleId.BOOKING_REBOOKED,
            berth=target_berth,
            ship=rebooked.ship,
            booking_id=rebooked.booking_id,
            reason=(
                f"原预约在 {booking.state.value} 后重新安排到 "
                f"{target_berth} {target_start}..{target_end} (v{rebooked.version})"
            ),
        )

    def cancel(self, booking_id: str) -> Decision:
        booking = self._require_booking(booking_id)
        if booking.state is BookingState.HONORED:
            raise BookingWindowConflict(
                f"booking {booking_id} is physically honoured; vacate the berth instead"
            )
        cancelled = booking.as_state(BookingState.CANCELLED)
        self._repo.bookings.save(cancelled)
        return self._record(
            action="cancel",
            outcome=Outcome.CANCELLED,
            rule=RuleId.BOOKING_CANCELLED,
            berth=booking.berth,
            ship=booking.ship,
            booking_id=booking.booking_id,
            reason="预约显式取消",
        )

    # ------------------------------------------------------------------ #
    # Reserved ship arrives
    # ------------------------------------------------------------------ #

    def arrive(self, booking_id: str) -> Decision:
        """A reserved vessel physically comes alongside within its window."""

        moment = self._clock.now()
        booking = self._require_booking(booking_id)
        if booking.state is not BookingState.HELD:
            raise ReservationNotReady(
                f"booking {booking_id} is {booking.state.value}"
            )
        if moment < booking.start:
            raise ReservationNotReady(
                f"booking {booking_id} starts at {booking.start}, now is {moment}"
            )
        if moment >= booking.grace_deadline():
            raise ReservationNotReady(
                f"booking {booking_id} passed grace deadline {booking.grace_deadline()}"
            )

        if not self._occupancy.is_free(booking.berth):
            occupant = self._occupancy.occupant(booking.berth) or "unknown"
            raise ReservationNotReady(
                f"berth {booking.berth} physically occupied by {occupant}"
            )

        honoured = booking.as_state(BookingState.HONORED)
        self._repo.bookings.save(honoured)
        self._occupancy.occupy(booking.berth, booking.ship)
        return self._record(
            action="reservation_arrival",
            outcome=Outcome.ACCEPTED,
            rule=RuleId.RESERVATION_HONORED,
            berth=booking.berth,
            ship=booking.ship,
            booking_id=booking.booking_id,
            reason=f"在窗口内到港并兑现，生效时刻 {moment}",
        )

    # ------------------------------------------------------------------ #
    # Walk-in traffic
    # ------------------------------------------------------------------ #

    def walk_in(
        self,
        ship: str,
        stay_end: str | None = None,
        preferred_berth: str = "",
        enqueue_if_blocked: bool = True,
    ) -> Decision:
        """Ad-hoc mooring: fit a gap, or explicitly pre-empt a future booking.

        Selection order for each berth:

        1. physically free + no held booking at all        -> R20
        2. physically free + all held bookings are future
           and the walk-in fits before the earliest one     -> R21
        3. physically free + only future held bookings      -> R22 (bump)
        Berths that are occupied or locked by an active /
        in-grace reservation are skipped.
        """

        moment = self._clock.now()
        end = stay_end or OPEN_ENDED
        if preferred_berth:
            self._require_berth(preferred_berth)
        if end <= moment:
            raise ValueError("walk-in stay end must be in the future")

        # Timeouts are always settled first, otherwise a lapsed booking could
        # be "bumped" by a walk-in instead of vacating under its own rule.
        self.sweep_timeouts()

        choice = self._select_berth(moment, end, preferred_berth)

        if choice is None:
            if enqueue_if_blocked:
                waiter = Waiter(
                    waiter_id=self._repo.new_waiter_id(),
                    ship=ship,
                    requested_at=moment,
                    preferred_berth=preferred_berth,
                )
                self._repo.queue.enqueue(waiter)
                return self._record(
                    action="walkin",
                    outcome=Outcome.QUEUED,
                    rule=RuleId.WALKIN_QUEUED,
                    berth=preferred_berth,
                    ship=ship,
                    waiter_id=waiter.waiter_id,
                    reason="无空窗且现存预约不可挤，进入等待队列",
                )
            return self._record(
                action="walkin",
                outcome=Outcome.DENIED,
                rule=RuleId.WALKIN_DENIED,
                berth=preferred_berth,
                ship=ship,
                reason="全部泊位锁定或占用且未允许排队",
            )

        berth, mode, victims = choice
        if mode == "free":
            self._occupancy.occupy(berth, ship)
            return self._record(
                action="walkin",
                outcome=Outcome.ACCEPTED,
                rule=RuleId.WALKIN_FREE_BERTH,
                berth=berth,
                ship=ship,
                reason="泊位当前空闲",
            )

        if mode == "gap":
            self._occupancy.occupy(berth, ship)
            return self._record(
                action="walkin",
                outcome=Outcome.ACCEPTED,
                rule=RuleId.WALKIN_FIT_GAP,
                berth=berth,
                ship=ship,
                related=tuple(booking.booking_id for booking in victims),
                reason=(
                    f"排入预约 {victims[0].booking_id} 前的空窗，"
                    f"须于 {victims[0].start} 前离开"
                ),
            )

        # Pre-emption: every held booking that the walk-in crosses is displaced.
        for booking in victims:
            bumped = booking.as_state(BookingState.BUMPED)
            self._repo.bookings.save(bumped)
        self._occupancy.occupy(berth, ship)
        decision = self._record(
            action="walkin",
            outcome=Outcome.PREEMPTED,
            rule=RuleId.WALKIN_PREEMPTS,
            berth=berth,
            ship=ship,
            related=tuple(booking.booking_id for booking in victims),
            reason="无空窗；仅存在未生效的未来预约，按 R22 明确挤掉",
        )
        for booking in victims:
            self._outbox.add(self._bump_notice(decision, booking))
        return decision

    def cancel_wait(self, waiter_id: str) -> Decision:
        waiter = self._repo.queue.get(waiter_id)
        if waiter is None:
            raise BookingNotFound(f"unknown waiter {waiter_id}")
        self._repo.queue.update(waiter.as_state(WaiterState.CANCELLED))
        return self._record(
            action="wait_cancel",
            outcome=Outcome.CANCELLED,
            rule=RuleId.WALKIN_WITHDRAWN,
            berth=waiter.preferred_berth,
            ship=waiter.ship,
            waiter_id=waiter.waiter_id,
            reason="船方撤销排队",
        )

    # ------------------------------------------------------------------ #
    # Timeouts, departure and queue promotion
    # ------------------------------------------------------------------ #

    def sweep_timeouts(self) -> list[Decision]:
        """Expire held bookings past start+grace; hand their berths onward.

        Expired bookings stay in the store as ``EXPIRED`` (rebookable), so the
        reservation never vanishes. The queue head is promoted automatically.
        """

        moment = self._clock.now()
        decisions: list[Decision] = []
        for booking in self._repo.bookings.all():
            if not booking.lapsed_at(moment):
                continue
            expired = booking.as_state(BookingState.EXPIRED)
            self._repo.bookings.save(expired)
            decision = self._record(
                action="no_show",
                outcome=Outcome.EXPIRED,
                rule=RuleId.RESERVATION_NO_SHOW,
                berth=booking.berth,
                ship=booking.ship,
                booking_id=booking.booking_id,
                displaced_booking_id=booking.booking_id,
                displaced_ship=booking.ship,
                reason=(
                    f"超过到点 {booking.start} + 宽限 {booking.grace_minutes} 分钟"
                    f"（截止 {booking.grace_deadline()}）仍未到，自动让位"
                ),
            )
            self._outbox.add(Notice.expired(decision, self._outbox.next_sequence()))
            decisions.append(decision)
            decisions.extend(self._promote_one(booking.berth))
        return decisions

    def vacate(self, berth: str) -> Decision:
        occupant = self._occupancy.occupant(berth)
        if occupant is None:
            raise ReservationNotReady(f"berth {berth} is already free")

        # An HONORED booking is historical evidence once the ship departs; it is
        # left untouched so the audit chain can still explain the occupancy.
        self._occupancy.release(berth)
        decision = self._record(
            action="vacate",
            outcome=Outcome.VACATED,
            rule=RuleId.BERTH_VACATED,
            berth=berth,
            ship=occupant,
            reason="实际靠泊船舶离港，泊位释放",
        )
        self._promote_all(berth)
        return decision

    # ------------------------------------------------------------------ #
    # Read models
    # ------------------------------------------------------------------ #

    def bookings(self) -> list[Booking]:
        return self._repo.bookings.all()

    def waiters(self) -> list[Waiter]:
        return self._repo.queue.all()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _select_berth(
        self, moment: str, end: str, preferred: str
    ) -> tuple[str, str, list[Booking]] | None:
        """Return ``(berth, mode, bookings)`` with the cheapest valid option.

        Mode is ``free`` / ``gap`` / ``bump``. Tiers are evaluated in that
        order across all candidate berths so a free berth is never rejected
        in favour of bumping someone.
        """

        codes = self._catalog.codes()
        candidates = [code for code in codes if not preferred or code == preferred]

        scored: list[tuple[int, str, str, list[Booking]]] = []
        for berth in candidates:
            if not self._occupancy.is_free(berth):
                continue
            held = self._repo.bookings.for_berth(
                berth, frozenset({BookingState.HELD})
            )
            if not held:
                scored.append((0, "free", berth, []))
                continue
            earliest = min(held, key=lambda item: item.start)
            if earliest.in_grace_at(moment) or earliest.active_at(moment):
                continue  # R11/R12: locked
            if moment < earliest.start:
                if end <= earliest.start:
                    scored.append((1, "gap", berth, [earliest]))
                else:
                    # Only reservations actually crossed by the walk-in are
                    # displaced; disjoint later windows survive untouched.
                    overlap = sorted(
                        (
                            booking
                            for booking in held
                            if booking.start < end
                        ),
                        key=lambda item: item.start,
                    )
                    scored.append((2, "bump", berth, overlap))
        if not scored:
            return None
        tier, mode, berth, bookings = min(scored, key=lambda item: (item[0], item[2]))
        return berth, mode, bookings

    def _promote_one(self, berth: str) -> list[Decision]:
        """Promote the first accepting waiter onto ``berth`` when it is free.

        Any HELD reservation blocks automation: a future reservation is only
        displaceable by an explicit R22 walk-in, never by silent promotion.
        A lapsed booking is EXPIRED by the time this runs, so it no longer
        blocks the handoff.
        """

        if not self._occupancy.is_free(berth):
            return []
        if self._repo.bookings.for_berth(
            berth, frozenset({BookingState.HELD})
        ):
            return []
        for waiter in self._repo.queue.waiting():
            if not waiter.accepts(berth):
                continue
            updated = waiter.promoted(berth)
            self._repo.queue.update(updated)
            self._occupancy.occupy(berth, waiter.ship)
            decision = self._record(
                action="queue_promotion",
                outcome=Outcome.PROMOTED,
                rule=RuleId.QUEUE_PROMOTED,
                berth=berth,
                ship=waiter.ship,
                waiter_id=waiter.waiter_id,
                reason="等待队列首位自动提升到空出泊位",
            )
            self._outbox.add(
                Notice.promoted(decision, waiter.ship, self._outbox.next_sequence())
            )
            return [decision]
        return []

    def _promote_all(self, berth: str) -> None:
        # One physical berth frees one slot; loop defensively in case the
        # occupancy state was adjusted by a preceding promotion.
        while self._promote_one(berth):
            pass

    def _window_conflict(
        self, berth: str, start: str, end: str, ignore: str = ""
    ) -> Booking | None:
        for booking in self._repo.bookings.for_berth(
            berth, frozenset({BookingState.HELD})
        ):
            if booking.booking_id == ignore:
                continue
            if booking.overlaps(start, end):
                return booking
        return None

    def _require_berth(self, berth: str) -> None:
        if not self._catalog.has(berth):
            raise LookupError(f"unknown berth {berth!r}")

    def _require_booking(self, booking_id: str) -> Booking:
        booking = self._repo.bookings.get(booking_id)
        if booking is None:
            raise BookingNotFound(f"unknown booking {booking_id}")
        return booking

    def _decide(
        self,
        *,
        action: str,
        outcome: Outcome,
        rule: RuleId,
        berth: str,
        ship: str,
        booking_id: str = "",
        waiter_id: str = "",
        displaced_booking_id: str = "",
        displaced_ship: str = "",
        reason: str = "",
        related: tuple[str, ...] = (),
    ) -> Decision:
        return Decision(
            action=action,
            outcome=outcome,
            rule=rule,
            effective_at=self._clock.now(),
            berth=berth,
            ship=ship,
            booking_id=booking_id,
            waiter_id=waiter_id,
            displaced_booking_id=displaced_booking_id,
            displaced_ship=displaced_ship,
            promoted_waiter_id=waiter_id if outcome is Outcome.PROMOTED else "",
            reason=reason,
            related=related,
        )

    def _record(self, **kwargs) -> Decision:
        decision = self._decide(**kwargs)
        self._ledger.append("berth.arbitration", self._audit_text(decision))
        return decision

    @staticmethod
    def _audit_text(decision: Decision) -> str:
        parts = [
            f"rule={decision.rule.value}",
            f"at={decision.effective_at}",
            f"outcome={decision.outcome.value}",
            f"berth={decision.berth}",
            f"ship={decision.ship}",
        ]
        if decision.booking_id:
            parts.append(f"booking={decision.booking_id}")
        if decision.waiter_id:
            parts.append(f"waiter={decision.waiter_id}")
        if decision.displaced_booking_id:
            parts.append(f"displaced={decision.displaced_booking_id}")
        if decision.reason:
            parts.append(decision.reason)
        return " ".join(parts)

    def _bump_notice(self, decision: Decision, booking: Booking) -> Notice:
        return Notice(
            notice_id=f"N{self._outbox.next_sequence():06d}",
            at=decision.effective_at,
            recipient_ship=booking.ship,
            kind="booking_bumped",
            rule=decision.rule.value,
            berth=decision.berth,
            booking_id=booking.booking_id,
            detail=(
                f"预约 {booking.booking_id} 在泊位 {decision.berth} "
                f"被临时靠泊 {decision.ship} 挤掉，已进入可重排池。"
            ),
        )
