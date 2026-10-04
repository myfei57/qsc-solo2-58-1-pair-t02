"""Outbox of results that must reach the party affected by a decision.

A pre-emption (R22) or a no-show handoff (R30) is only complete once the
displaced party *receives* the result. Decisions append a ``Notice`` here;
delivery integrations mark notices delivered after the fact, so nothing is
lost or "settled by shouting" on the quay.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum

from portpower.berth.arbitration.decision import Decision
from portpower.store.kvstore import RecordStore

OUTBOX_KEY = "berth.notices"


class NoticeStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"


@dataclass(frozen=True)
class Notice:
    notice_id: str
    at: str
    recipient_ship: str
    kind: str           # "booking_bumped" | "booking_expired" | "queue_promoted"
    rule: str
    berth: str
    booking_id: str = ""
    waiter_id: str = ""
    detail: str = ""
    status: NoticeStatus = NoticeStatus.PENDING

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> "Notice | None":
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                notice_id=str(payload["notice_id"]),
                at=str(payload["at"]),
                recipient_ship=str(payload["recipient_ship"]),
                kind=str(payload["kind"]),
                rule=str(payload["rule"]),
                berth=str(payload["berth"]),
                booking_id=str(payload.get("booking_id", "")),
                waiter_id=str(payload.get("waiter_id", "")),
                detail=str(payload.get("detail", "")),
                status=NoticeStatus(
                    str(payload.get("status", NoticeStatus.PENDING.value))
                ),
            )
        except (KeyError, ValueError, TypeError):
            return None

    @classmethod
    def expired(cls, decision: Decision, sequence: int) -> "Notice":
        return cls(
            notice_id=f"N{sequence:06d}",
            at=decision.effective_at,
            recipient_ship=decision.displaced_ship,
            kind="booking_expired",
            rule=decision.rule.value,
            berth=decision.berth,
            booking_id=decision.displaced_booking_id,
            detail=(
                f"预约 {decision.displaced_booking_id} 超时未到，泊位 "
                f"{decision.berth} 已让位；预约保留在可重排池。"
            ),
        )

    @classmethod
    def promoted(cls, decision: Decision, ship: str, sequence: int) -> "Notice":
        return cls(
            notice_id=f"N{sequence:06d}",
            at=decision.effective_at,
            recipient_ship=ship,
            kind="queue_promoted",
            rule=decision.rule.value,
            berth=decision.berth,
            waiter_id=decision.promoted_waiter_id,
            detail=f"等待队列已将您提升到泊位 {decision.berth}，请立即靠泊。",
        )


class Outbox:
    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def add(self, notice: Notice) -> None:
        def mutate(document: object) -> tuple[object, None]:
            items = self._normalize(document)
            items = [item for item in items if item.notice_id != notice.notice_id]
            items.append(notice)
            items.sort(key=lambda item: item.at)
            return [item.as_dict() for item in items], None

        self._store.update_json(OUTBOX_KEY, mutate)

    def next_sequence(self) -> int:
        return len(self._normalize(self._store.get_json(OUTBOX_KEY))) + 1

    def list(self, ship: str | None = None) -> list[Notice]:
        items = self._normalize(self._store.get_json(OUTBOX_KEY))
        if ship:
            items = [item for item in items if item.recipient_ship == ship]
        return items

    def pending(self, ship: str | None = None) -> list[Notice]:
        return [
            item
            for item in self.list(ship)
            if item.status is NoticeStatus.PENDING
        ]

    def mark_delivered(self, notice_id: str) -> bool:
        found = {"value": False}

        def mutate(document: object) -> tuple[object, None]:
            items = self._normalize(document)
            for index, item in enumerate(items):
                if item.notice_id == notice_id:
                    items[index] = replace(item, status=NoticeStatus.DELIVERED)
                    found["value"] = True
            if not found["value"]:
                return None, None
            return [item.as_dict() for item in items], None

        self._store.update_json(OUTBOX_KEY, mutate)
        return found["value"]

    @staticmethod
    def _normalize(document: object) -> list[Notice]:
        if not isinstance(document, list):
            return []
        return [
            notice
            for item in document
            if (notice := Notice.from_dict(item)) is not None
        ]
