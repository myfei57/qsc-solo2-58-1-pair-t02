"""Append-only journal of berth arbitration decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from portpower.store.kvstore import RecordStore

LOG_KEY = "berth.decisions"


class Outcome(str, Enum):
    BOOKED = "booked"
    ASSIGNED = "assigned"
    QUEUED = "queued"
    PREEMPTED = "preempted"
    EXPIRED = "expired"
    REBOOKED = "rebooked"
    PROMOTED = "promoted"
    RELEASED = "released"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"
    DROPPED = "dropped"


@dataclass(frozen=True)
class Decision:
    """One ruling: who, where, under which rule, effective from when."""

    seq: int
    at: str
    berth: str
    ship: str
    rule: str
    outcome: str
    effective_at: str
    reason: str = ""
    booking: str | None = None
    displaced: str | None = None
    ticket: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "seq": self.seq,
            "at": self.at,
            "berth": self.berth,
            "ship": self.ship,
            "rule": self.rule,
            "outcome": self.outcome,
            "effective_at": self.effective_at,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.booking is not None:
            payload["booking"] = self.booking
        if self.displaced is not None:
            payload["displaced"] = self.displaced
        if self.ticket is not None:
            payload["ticket"] = self.ticket
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> "Decision | None":
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                seq=int(payload["seq"]),
                at=str(payload["at"]),
                berth=str(payload["berth"]),
                ship=str(payload["ship"]),
                rule=str(payload["rule"]),
                outcome=str(payload["outcome"]),
                effective_at=str(payload["effective_at"]),
                reason=str(payload.get("reason") or ""),
                booking=_optional_text(payload.get("booking")),
                displaced=_optional_text(payload.get("displaced")),
                ticket=_optional_text(payload.get("ticket")),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class DecisionLog:
    """Durable, queryable record of every ruling the arbiter makes."""

    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def record(
        self,
        *,
        at: str,
        berth: str,
        ship: str,
        rule: str,
        outcome: str,
        effective_at: str,
        reason: str = "",
        booking: str | None = None,
        displaced: str | None = None,
        ticket: str | None = None,
    ) -> Decision:
        def mutate(document: object) -> tuple[object, Decision]:
            entries = self._normalize(document)
            decision = Decision(
                seq=len(entries) + 1,
                at=at,
                berth=berth,
                ship=ship,
                rule=rule,
                outcome=outcome,
                effective_at=effective_at,
                reason=reason,
                booking=booking,
                displaced=displaced,
                ticket=ticket,
            )
            entries.append(decision)
            return [item.as_dict() for item in entries], decision

        return self._store.update_json(LOG_KEY, mutate)

    def all(self) -> list[Decision]:
        return self._normalize(self._store.get_json(LOG_KEY))

    def latest(self) -> Decision | None:
        entries = self.all()
        return entries[-1] if entries else None

    def for_berth(self, berth: str) -> list[Decision]:
        return [entry for entry in self.all() if entry.berth == berth]

    def for_ship(self, ship: str) -> list[Decision]:
        return [entry for entry in self.all() if entry.ship == ship]

    def for_rule(self, rule: str) -> list[Decision]:
        return [entry for entry in self.all() if entry.rule == rule]

    def for_booking(self, booking: str) -> list[Decision]:
        return [entry for entry in self.all() if entry.booking == booking]

    @staticmethod
    def _normalize(document: object) -> list[Decision]:
        if not isinstance(document, list):
            return []
        entries: list[Decision] = []
        for item in document:
            decision = Decision.from_dict(item)
            if decision is not None:
                entries.append(decision)
        entries.sort(key=lambda item: item.seq)
        return entries
