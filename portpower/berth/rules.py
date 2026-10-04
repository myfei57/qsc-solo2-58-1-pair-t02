"""Named arbitration rules; every berth decision cites exactly one."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Rule:
    code: str
    summary: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


BOOKING_ACCEPTED = Rule(
    "R0-booking-accepted",
    "window was free; the reservation now holds it",
)
RESERVED_WINDOW = Rule(
    "R1-reserved-window",
    "reserved ship arrived inside its window and takes the berth",
)
WALK_IN_GAP = Rule(
    "R2-walk-in-gap",
    "walk-in placed only where no booked reservation needs the window",
)
EXPLICIT_PREEMPTION = Rule(
    "R3-explicit-preemption",
    "caller displaced a booked reservation by naming a reason",
)
NO_SHOW_TIMEOUT = Rule(
    "R4-no-show-timeout",
    "reservation missed its grace deadline and yields the berth",
)
REBOOK = Rule(
    "R5-rebook",
    "preempted or expired reservation is given a new window",
)
QUEUE_PROMOTION = Rule(
    "R6-queue-promotion",
    "freed berth goes to the first queued request that fits",
)
RECORD_KEEPING = Rule(
    "R7-record-keeping",
    "vacate, cancellation or stale-queue cleanup",
)

ALL: tuple[Rule, ...] = (
    BOOKING_ACCEPTED,
    RESERVED_WINDOW,
    WALK_IN_GAP,
    EXPLICIT_PREEMPTION,
    NO_SHOW_TIMEOUT,
    REBOOK,
    QUEUE_PROMOTION,
    RECORD_KEEPING,
)

_BY_CODE = {rule.code: rule for rule in ALL}


def describe(code: str) -> Rule | None:
    return _BY_CODE.get(code)
