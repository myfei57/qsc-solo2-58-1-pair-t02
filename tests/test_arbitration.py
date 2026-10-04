"""End-to-end arbitration tests: one rule set for reservations and walk-ins."""

from __future__ import annotations

import tempfile
import unittest

from portpower.audit.ledger import Ledger
from portpower.berth.arbitration.arbiter import (
    Arbiter,
    BookingNotFound,
    BookingWindowConflict,
    ReservationNotReady,
)
from portpower.berth.arbitration.booking import BookingState
from portpower.berth.arbitration.clock import FixedClock
from portpower.berth.arbitration.decision import Outcome
from portpower.berth.arbitration.notice import NoticeStatus, Outbox
from portpower.berth.arbitration.occupancy import Occupancy
from portpower.berth.arbitration.repository import ArbitrationRepository
from portpower.berth.arbitration.rules import RuleId
from portpower.berth.arbitration.sweeper import TimeoutSweeper
from portpower.berth.mapping import BerthMapping
from portpower.berth.registry import BerthRegistry
from portpower.ns.catalog import Catalog
from portpower.store.kvstore import RecordStore

T0 = "2026-10-03T08:00:00+00:00"
T1 = "2026-10-03T09:00:00+00:00"
T2 = "2026-10-03T10:00:00+00:00"
T3 = "2026-10-03T11:00:00+00:00"
T4 = "2026-10-03T12:00:00+00:00"
T5 = "2026-10-03T13:00:00+00:00"
T_GRACE = "2026-10-03T09:15:00+00:00"
T_AFTER_GRACE = "2026-10-03T09:16:00+00:00"


class ArbiterHarness:
    def __init__(self, clock_at: str = T0) -> None:
        self.dir = tempfile.mkdtemp()
        self.store = RecordStore(self.dir)
        self.store.load()
        self.clock = FixedClock(clock_at)
        self.catalog = Catalog(("A1", "A2", "B1"))
        registry = BerthRegistry()
        for code in self.catalog.codes():
            registry.add(code)
        self.occupancy = Occupancy(registry, BerthMapping(self.store))
        self.repo = ArbitrationRepository(self.store)
        self.ledger = Ledger(self.store)
        self.outbox = Outbox(self.store)
        self.arbiter = Arbiter(
            self.repo, self.occupancy, self.catalog, self.outbox, self.ledger, self.clock
        )

    def at(self, moment: str):
        self.clock.set(moment)
        return self

    def occupy_others(self, *codes: str) -> None:
        """Block every catalog berth except the ones still under test."""

        for index, code in enumerate(codes):
            self.occupancy.occupy(code, f"占位船{index}")

    def fill_all_but(self, free: str) -> None:
        for code in self.catalog.codes():
            if code != free:
                self.occupancy.occupy(code, f"占位-{code}")


class BookingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = ArbiterHarness(T0)

    def test_booking_accepted_is_held_and_cites_rule_and_time(self):
        decision = self.h.arbiter.book("船甲", "A1", T1, T3)
        self.assertEqual(decision.outcome, Outcome.ACCEPTED)
        self.assertEqual(decision.rule, RuleId.BOOKING_ACCEPTED)
        self.assertEqual(decision.effective_at, T0)
        booking = self.h.repo.bookings.get(decision.booking_id)
        self.assertEqual(booking.state, BookingState.HELD)
        line = self.h.ledger.latest()
        self.assertIn("rule=R1", line.detail)
        self.assertIn(f"at={T0}", line.detail)

    def test_overlapping_booking_rejected_and_audited(self):
        self.h.arbiter.book("船甲", "A1", T1, T3)
        with self.assertRaises(BookingWindowConflict):
            self.h.arbiter.book("船乙", "A1", T2, T4)
        self.assertEqual(self.h.ledger.latest().event, "berth.arbitration")
        self.assertIn("rule=R2", self.h.ledger.latest().detail)

    def test_different_berths_do_not_conflict(self):
        self.h.arbiter.book("船甲", "A1", T1, T3)
        decision = self.h.arbiter.book("船乙", "A2", T1, T3)
        self.assertEqual(decision.rule, RuleId.BOOKING_ACCEPTED)


class WalkInTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = ArbiterHarness(T0)

    def test_walkin_takes_free_berth_r20(self):
        decision = self.h.arbiter.walk_in("临船1")
        self.assertEqual(decision.outcome, Outcome.ACCEPTED)
        self.assertEqual(decision.rule, RuleId.WALKIN_FREE_BERTH)
        self.assertEqual(decision.berth, "A1")
        self.assertEqual(self.h.occupancy.occupant("A1"), "临船1")

    def test_walkin_fits_gap_before_future_reservation_r21(self):
        self.h.fill_all_but("A1")
        self.h.arbiter.book("预约船", "A1", T3, T5)
        decision = self.h.arbiter.walk_in("临船1", stay_end=T3)
        self.assertEqual(decision.rule, RuleId.WALKIN_FIT_GAP)
        self.assertEqual(decision.berth, "A1")

    def test_walkin_preempts_future_booking_r22_and_notices(self):
        self.h.fill_all_but("A1")
        book = self.h.arbiter.book("预约船", "A1", T3, T5)
        decision = self.h.arbiter.walk_in("临船1")  # open-ended, crosses T3
        self.assertEqual(decision.outcome, Outcome.PREEMPTED)
        self.assertEqual(decision.rule, RuleId.WALKIN_PREEMPTS)
        self.assertEqual(decision.effective_at, T0)
        # booking survives, now BUMPED and rebookable
        bumped = self.h.repo.bookings.get(book.booking_id)
        self.assertEqual(bumped.state, BookingState.BUMPED)
        self.assertTrue(bumped.rebookable)
        # displaced party has a pending result in the outbox
        notices = self.h.outbox.pending("预约船")
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].kind, "booking_bumped")
        self.assertEqual(notices[0].rule, "R22")
        self.assertEqual(notices[0].booking_id, book.booking_id)
        self.assertEqual(notices[0].status, NoticeStatus.PENDING)
        self.assertEqual(self.h.occupancy.occupant("A1"), "临船1")

    def test_free_berth_preferred_over_bumping(self):
        self.h.arbiter.book("预约船", "A1", T3, T5)  # A1 bumpable
        # A2 has no booking at all -> must win via R20, no bump
        decision = self.h.arbiter.walk_in("临船1")
        self.assertEqual(decision.rule, RuleId.WALKIN_FREE_BERTH)
        self.assertEqual(decision.berth, "A2")
        self.assertEqual(self.h.outbox.pending(), [])

    def test_walkin_cannot_bump_active_or_grace_reservation(self):
        # within the grace hold (start..start+15m) the berth is locked: R12
        self.h.fill_all_but("A1")
        self.h.arbiter.book("预约船", "A1", T1, T3)
        self.h.at("2026-10-03T09:10:00+00:00")
        decision = self.h.arbiter.walk_in("临船1")
        self.assertEqual(decision.outcome, Outcome.QUEUED)
        self.assertEqual(decision.rule, RuleId.WALKIN_QUEUED)
        self.assertIsNone(self.h.occupancy.occupant("A1"))

        # past the grace deadline the reservation auto-vacates first, so a
        # walk-in landing at 10:00 takes the freed berth (R20 after R30)
        h2 = ArbiterHarness("2026-10-03T09:10:00+00:00")
        h2.occupancy.occupy("A2", "占位-A2")
        h2.occupancy.occupy("B1", "占位-B1")
        book = h2.arbiter.book("预约船B", "A1", T1, T3)
        h2.at(T2)
        decision = h2.arbiter.walk_in("临船2")
        self.assertEqual(decision.rule, RuleId.WALKIN_FREE_BERTH)
        self.assertEqual(
            h2.repo.bookings.get(book.booking_id).state, BookingState.EXPIRED
        )
        self.assertEqual(len(h2.outbox.pending("预约船B")), 1)

    def test_denied_when_queue_refused(self):
        # every berth physically occupied, nothing can be queued into
        for code in ("A1", "A2", "B1"):
            self.h.occupancy.occupy(code, f"在场-{code}")
        self.h.at(T2)
        decision = self.h.arbiter.walk_in("临船1", enqueue_if_blocked=False)
        self.assertEqual(decision.outcome, Outcome.DENIED)
        self.assertEqual(decision.rule, RuleId.WALKIN_DENIED)


class ReservationArrivalTests(unittest.TestCase):
    def test_arrival_inside_window_honoured_r10(self):
        h = ArbiterHarness(T0)
        book = h.arbiter.book("预约船", "A1", T1, T3)
        h.at(T1)
        decision = h.arbiter.arrive(book.booking_id)
        self.assertEqual(decision.rule, RuleId.RESERVATION_HONORED)
        self.assertEqual(h.occupancy.occupant("A1"), "预约船")
        self.assertEqual(
            h.repo.bookings.get(book.booking_id).state, BookingState.HONORED
        )

    def test_arrival_before_start_refused(self):
        h = ArbiterHarness(T0)
        book = h.arbiter.book("预约船", "A1", T1, T3)
        with self.assertRaises(ReservationNotReady):
            h.arbiter.arrive(book.booking_id)

    def test_arrival_after_grace_refused(self):
        h = ArbiterHarness(T0)
        book = h.arbiter.book("预约船", "A1", T1, T3)
        h.at(T_AFTER_GRACE)
        with self.assertRaises(ReservationNotReady):
            h.arbiter.arrive(book.booking_id)


class NoShowAndQueueTests(unittest.TestCase):
    def test_no_show_expires_frees_berth_keeps_booking_r30(self):
        h = ArbiterHarness(T0)
        book = h.arbiter.book("预约船", "A1", T1, T3)
        # a walk-in was queued earlier while the reservation held the berth
        h.fill_all_but("A1")
        h.at(T1)
        queued = h.arbiter.walk_in("排队船")
        self.assertEqual(queued.rule, RuleId.WALKIN_QUEUED)

        h.at(T_AFTER_GRACE)
        decisions = h.arbiter.sweep_timeouts()
        rules = {(d.rule, d.outcome) for d in decisions}
        self.assertIn((RuleId.RESERVATION_NO_SHOW, Outcome.EXPIRED), rules)
        self.assertIn((RuleId.QUEUE_PROMOTED, Outcome.PROMOTED), rules)

        expired = h.repo.bookings.get(book.booking_id)
        self.assertEqual(expired.state, BookingState.EXPIRED)
        self.assertTrue(expired.rebookable)  # not deleted
        # next in queue physically took the berth
        self.assertEqual(h.occupancy.occupant("A1"), "排队船")
        waiter = h.repo.queue.get(queued.waiter_id)
        self.assertEqual(waiter.assigned_berth, "A1")
        # both parties informed
        self.assertEqual(len(h.outbox.pending("预约船")), 1)
        self.assertEqual(h.outbox.pending("预约船")[0].kind, "booking_expired")
        self.assertEqual(len(h.outbox.pending("排队船")), 1)
        self.assertEqual(h.outbox.pending("排队船")[0].kind, "queue_promoted")

    def test_vacate_promotes_waiter_r40_r31(self):
        h = ArbiterHarness(T0)
        h.occupancy.occupy("A1", "在场船")
        queued = h.arbiter.walk_in("排队船", preferred_berth="A1")
        self.assertEqual(queued.outcome, Outcome.QUEUED)
        decision = h.arbiter.vacate("A1")
        self.assertEqual(decision.rule, RuleId.BERTH_VACATED)
        self.assertEqual(h.occupancy.occupant("A1"), "排队船")
        self.assertEqual(len(h.outbox.pending("排队船")), 1)

    def test_queue_is_fifo_and_respects_berth_preference(self):
        h = ArbiterHarness(T0)
        h.occupancy.occupy("A1", "在场船1")
        h.occupancy.occupy("A2", "在场船2")
        h.occupancy.occupy("B1", "在场船3")
        w1 = h.arbiter.walk_in("只要A1", preferred_berth="A1")
        w2 = h.arbiter.walk_in("随便哪")
        h.arbiter.vacate("A2")  # w1 rejects A2, w2 gets it
        self.assertEqual(h.occupancy.occupant("A2"), "随便哪")
        self.assertEqual(h.occupancy.occupant("A1"), "在场船1")  # still held
        h.arbiter.vacate("A1")
        self.assertEqual(h.occupancy.occupant("A1"), "只要A1")
        self.assertEqual(
            h.repo.queue.get(w1.waiter_id).state.value, "promoted"
        )
        self.assertEqual(
            h.repo.queue.get(w2.waiter_id).state.value, "promoted"
        )


class RebookTests(unittest.TestCase):
    def test_bumped_booking_can_be_rearranged_r4(self):
        h = ArbiterHarness(T0)
        h.occupancy.occupy("A2", "占位-A2")
        h.occupancy.occupy("B1", "占位-B1")
        book = h.arbiter.book("预约船", "A1", T1, T3)
        h.arbiter.walk_in("临船1", preferred_berth="A1")  # bumps via R22
        decision = h.arbiter.rebook(book.booking_id, berth="A2", start=T3, end=T5)
        self.assertEqual(decision.rule, RuleId.BOOKING_REBOOKED)
        rebooked = h.repo.bookings.get(book.booking_id)
        self.assertEqual(rebooked.state, BookingState.HELD)
        self.assertEqual(rebooked.berth, "A2")
        self.assertEqual(rebooked.version, 2)

    def test_expired_booking_can_be_rearranged(self):
        h = ArbiterHarness(T0)
        h.fill_all_but("A1")
        book = h.arbiter.book("预约船", "A1", T1, T3)
        h.at(T_AFTER_GRACE)
        h.arbiter.sweep_timeouts()
        # sweep did not auto-promote: no queued waiter existed in this case
        decision = h.arbiter.rebook(book.booking_id, start=T4, end=T5)
        self.assertEqual(decision.rule, RuleId.BOOKING_REBOOKED)
        self.assertEqual(
            h.repo.bookings.get(book.booking_id).state, BookingState.HELD
        )

    def test_held_booking_is_not_rebookable(self):
        h = ArbiterHarness(T0)
        book = h.arbiter.book("预约船", "A1", T1, T3)
        with self.assertRaises(BookingNotFound):
            h.arbiter.rebook(book.booking_id, berth="A2")

    def test_rebook_into_occupied_window_conflicts(self):
        h = ArbiterHarness(T0)
        h.fill_all_but("A1")
        book = h.arbiter.book("预约船", "A1", T1, T3)
        h.arbiter.book("占窗船", "A2", T3, T5)
        h.arbiter.walk_in("临船1")  # bumps A1 booking
        with self.assertRaises(BookingWindowConflict):
            h.arbiter.rebook(book.booking_id, berth="A2", start=T4, end=T5)


class TraceabilityTests(unittest.TestCase):
    def test_every_preemption_is_audited_with_rule_and_instant(self):
        h = ArbiterHarness(T0)
        h.fill_all_but("A1")
        h.arbiter.book("预约船", "A1", T1, T3)
        decision = h.arbiter.walk_in("临船1", preferred_berth="A1")
        entries = h.ledger.entries()
        self.assertTrue(
            any(
                f"rule={decision.rule.value}" in e.detail
                and f"at={decision.effective_at}" in e.detail
                and "berth=A1" in e.detail
                for e in entries
            )
        )

    def test_state_survives_restart(self):
        import os

        d = tempfile.mkdtemp()
        store = RecordStore(d)
        store.load()
        clock = FixedClock(T0)
        catalog = Catalog(("A1",))
        registry = BerthRegistry()
        registry.add("A1")
        occupancy = Occupancy(registry, BerthMapping(store))
        repo = ArbitrationRepository(store)
        arbiter = Arbiter(
            repo, occupancy, catalog, Outbox(store), Ledger(store), clock
        )
        book = arbiter.book("预约船", "A1", T1, T3)
        arbiter.walk_in("临船1")

        # rebuild everything from disk
        store2 = RecordStore(d)
        store2.load()
        registry2 = BerthRegistry()
        registry2.add("A1")
        occupancy2 = Occupancy(registry2, BerthMapping(store2))
        occupancy2.hydrate()
        self.assertEqual(occupancy2.occupant("A1"), "临船1")
        repo2 = ArbitrationRepository(store2)
        self.assertEqual(
            repo2.bookings.get(book.booking_id).state, BookingState.BUMPED
        )


class SweeperTests(unittest.TestCase):
    def test_background_thread_vacates_no_show_without_incoming_call(self):
        import time

        h = ArbiterHarness(T0)
        book = h.arbiter.book("预约船", "A1", T1, T3)
        sweeper = TimeoutSweeper(h.arbiter, interval_seconds=0.01)
        sweeper.start()
        try:
            h.at(T_AFTER_GRACE)
            for _ in range(100):
                if h.repo.bookings.get(book.booking_id).state is BookingState.EXPIRED:
                    break
                time.sleep(0.02)
        finally:
            sweeper.stop()
        self.assertEqual(
            h.repo.bookings.get(book.booking_id).state, BookingState.EXPIRED
        )
        self.assertEqual(len(h.outbox.pending("预约船")), 1)


if __name__ == "__main__":
    unittest.main()
