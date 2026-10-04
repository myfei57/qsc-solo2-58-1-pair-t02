"""The verdict emitted by every arbitration entry point."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum

from portpower.berth.arbitration.rules import RULE_DESCRIPTIONS, RuleId


class Outcome(str, Enum):
    ACCEPTED = "accepted"
    PREEMPTED = "preempted"   # someone's booking was displaced
    QUEUED = "queued"
    DENIED = "denied"
    VACATED = "vacated"
    PROMOTED = "promoted"
    CANCELLED = "cancelled"
    REBOOKED = "rebooked"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Decision:
    """What was decided, under which rule, and the instant it took effect."""

    action: str                 # e.g. "walkin", "reservation_arrival", "sweep"
    outcome: Outcome
    rule: RuleId
    effective_at: str
    berth: str
    ship: str
    # Booking/waiter identifiers when the decision concerns one.
    booking_id: str = ""
    waiter_id: str = ""
    # Booking displaced by this decision (walk-in pre-emption / no-show handoff).
    displaced_booking_id: str = ""
    displaced_ship: str = ""
    # Booking that picked up the freed berth via queue promotion.
    promoted_waiter_id: str = ""
    reason: str = ""
    related: tuple[str, ...] = field(default_factory=tuple)

    def rule_text(self) -> str:
        return RULE_DESCRIPTIONS.get(self.rule, "")

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        payload["rule"] = self.rule.value
        payload["rule_text"] = self.rule_text()
        payload["related"] = list(self.related)
        return {key: value for key, value in payload.items() if value != ""}
