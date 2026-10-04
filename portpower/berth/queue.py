"""FIFO wait queue for berthing requests that found no window yet."""

from __future__ import annotations

from dataclasses import dataclass

from portpower.store.kvstore import RecordStore

QUEUE_KEY = "berth.queue"


@dataclass(frozen=True)
class QueuedRequest:
    """A ship waiting for a berth window to open up."""

    ticket: str
    ship: str
    berth: str
    start: str
    end: str
    enqueued_at: str
    booking: str | None = None
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ticket": self.ticket,
            "ship": self.ship,
            "berth": self.berth,
            "start": self.start,
            "end": self.end,
            "enqueued_at": self.enqueued_at,
        }
        if self.booking is not None:
            payload["booking"] = self.booking
        if self.note:
            payload["note"] = self.note
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> "QueuedRequest | None":
        if not isinstance(payload, dict):
            return None
        try:
            booking = payload.get("booking")
            return cls(
                ticket=str(payload["ticket"]),
                ship=str(payload["ship"]),
                berth=str(payload["berth"]),
                start=str(payload["start"]),
                end=str(payload["end"]),
                enqueued_at=str(payload["enqueued_at"]),
                booking=booking if isinstance(booking, str) and booking else None,
                note=str(payload.get("note") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


class WaitQueue:
    """Durable FIFO; promotion always scans from the head."""

    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def enqueue(self, entry: QueuedRequest) -> None:
        def mutate(document: object) -> tuple[object, QueuedRequest]:
            entries = self._normalize(document)
            entries.append(entry)
            return [item.as_dict() for item in entries], entry

        self._store.update_json(QUEUE_KEY, mutate)

    def remove(self, ticket: str) -> bool:
        def mutate(document: object) -> tuple[object, bool]:
            entries = self._normalize(document)
            kept = [item for item in entries if item.ticket != ticket]
            return [item.as_dict() for item in kept], len(kept) != len(entries)

        return bool(self._store.update_json(QUEUE_KEY, mutate))

    def entries(self) -> list[QueuedRequest]:
        return self._normalize(self._store.get_json(QUEUE_KEY))

    def position(self, ticket: str) -> int | None:
        for index, entry in enumerate(self.entries(), start=1):
            if entry.ticket == ticket:
                return index
        return None

    def count(self) -> int:
        return len(self.entries())

    @staticmethod
    def _normalize(document: object) -> list[QueuedRequest]:
        if not isinstance(document, list):
            return []
        entries: list[QueuedRequest] = []
        for item in document:
            entry = QueuedRequest.from_dict(item)
            if entry is not None:
                entries.append(entry)
        return entries
