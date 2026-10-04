"""A queued walk-in that found neither a free berth nor a usable gap."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class WaiterState(str, Enum):
    WAITING = "waiting"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Waiter:
    waiter_id: str
    ship: str
    requested_at: str
    # Optional desired berth; when empty the waiter accepts any catalog berth.
    preferred_berth: str = ""
    state: WaiterState = WaiterState.WAITING
    assigned_berth: str = ""

    def __post_init__(self) -> None:
        if not self.waiter_id.strip():
            raise ValueError("waiter id is required")
        if not self.ship.strip():
            raise ValueError("waiter ship is required")

    def accepts(self, berth: str) -> bool:
        return not self.preferred_berth or self.preferred_berth == berth

    def promoted(self, berth: str) -> "Waiter":
        return replace(
            self, state=WaiterState.PROMOTED, assigned_berth=berth
        )

    def as_state(self, state: WaiterState) -> "Waiter":
        return replace(self, state=state)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "waiter_id": self.waiter_id,
            "ship": self.ship,
            "requested_at": self.requested_at,
            "preferred_berth": self.preferred_berth,
            "state": self.state.value,
        }
        if self.assigned_berth:
            payload["assigned_berth"] = self.assigned_berth
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> "Waiter | None":
        if not isinstance(payload, dict):
            return None
        try:
            state = WaiterState(str(payload.get("state", WaiterState.WAITING.value)))
            return cls(
                waiter_id=str(payload["waiter_id"]),
                ship=str(payload["ship"]),
                requested_at=str(payload["requested_at"]),
                preferred_berth=str(payload.get("preferred_berth", "")),
                state=state,
                assigned_berth=str(payload.get("assigned_berth", "")),
            )
        except (KeyError, ValueError, TypeError):
            return None
