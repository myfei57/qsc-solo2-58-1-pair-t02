"""Physical occupancy gate-keeper: registry plus its durable mapping."""

from __future__ import annotations

from portpower.berth.mapping import BerthMapping
from portpower.berth.registry import BerthRegistry


class Occupancy:
    """Keeps the in-memory registry and the restart-safe mapping in step."""

    def __init__(self, registry: BerthRegistry, mapping: BerthMapping) -> None:
        self._registry = registry
        self._mapping = mapping

    def hydrate(self) -> None:
        """Rebuild registry occupancy from the durable mapping at startup."""

        for code, ship in self._mapping.entries().items():
            berth = self._registry.add(code)
            if not berth.occupied:
                berth.assign(ship)

    def is_free(self, berth: str) -> bool:
        current = self._registry.get(berth)
        return current is not None and not current.occupied

    def occupant(self, berth: str) -> str | None:
        return self._registry.ship_at(berth)

    def occupy(self, berth: str, ship: str) -> None:
        target = self._registry.add(berth)
        target.assign(ship)
        self._mapping.bind(berth, ship)

    def release(self, berth: str) -> str | None:
        occupant = self._registry.ship_at(berth)
        self._registry.release(berth)
        self._mapping.unbind(berth)
        return occupant
