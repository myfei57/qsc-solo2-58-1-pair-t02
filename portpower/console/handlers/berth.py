"""Berth inventory, occupancy and allocation endpoints.

The mutating endpoints are thin legacy wrappers over the unified
:mod:`portpower.berth.arbitration` engine, so ad-hoc allocation from the old
API is judged by exactly the same rules as ``/berth/walkin``.
"""

from __future__ import annotations

from portpower.berth.registry import NoBerthAvailable
from portpower.console.handlers.arbitration import reconcile_fleet
from portpower.console.http import Request, Response
from portpower.console.validation import optional_text, require_text


def berths(runtime, request: Request) -> Response:
    entries = runtime.mapping.entries()
    return Response.json(200, {"count": len(entries), "berths": entries})


def berth_status(runtime, request: Request) -> Response:
    statuses = runtime.berths.status_all()
    return Response.json(
        200,
        {
            "occupied_count": runtime.berths.occupied_count(),
            "count": len(statuses),
            "berths": [item.as_dict() for item in statuses],
        },
    )


def berth_detail(runtime, request: Request) -> Response:
    code = request.query_one("berth", "") or ""
    berth = runtime.berths.get(code)
    if berth is None:
        raise LookupError(f"unknown berth {code!r}")
    return Response.json(200, berth.describe())


def allocate(runtime, request: Request) -> Response:
    """Legacy ad-hoc allocation.

    Judged like a walk-in that does not accept a queue: it either lands in a
    free berth/gap, pre-empts a future booking (R22), or gets 409.
    """

    payload = request.json_object()
    ship = require_text(payload, "ship")
    preferred = optional_text(payload, "berth")
    stay_end = optional_text(payload, "stay_end") or None
    decision = runtime.arbiter.walk_in(
        ship=ship,
        stay_end=stay_end,
        preferred_berth=preferred,
        enqueue_if_blocked=False,
    )
    reconcile_fleet(runtime)
    if decision.outcome.value != "accepted":
        raise NoBerthAvailable(f"no berth available ({decision.rule.value})")
    return Response.json(200, {"berth": decision.berth, "decision": decision.as_dict()})


def release_berth(runtime, request: Request) -> Response:
    payload = request.json_object()
    code = require_text(payload, "berth")
    decision = runtime.arbiter.vacate(code)
    reconcile_fleet(runtime)
    return Response.json(200, {"vacated": True, "decision": decision.as_dict()})
