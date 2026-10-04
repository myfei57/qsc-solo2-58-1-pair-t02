"""Endpoints for the unified berth arbitration engine.

Reservations and ad-hoc moorings share these endpoints; every response carries
the ``Decision`` (rule id + effective timestamp) and displaced parties receive
notices in the outbox.
"""

from __future__ import annotations

from portpower.berth.arbitration.arbiter import (
    BookingNotFound,
    BookingWindowConflict,
    ReservationNotReady,
)
from portpower.berth.arbitration.booking import BOOKING_REBOOKABLE
from portpower.console.http import BadRequest, Request, Response
from portpower.console.validation import optional_number, optional_text, require_text


def _decision_payload(decision) -> dict[str, object]:
    return {"decision": decision.as_dict()}


def reconcile_fleet(runtime) -> None:
    """Keep the in-memory fleet view aligned with arbitrated occupancy."""

    docked: dict[str, str] = {}
    for code in runtime.catalog.codes():
        ship = runtime.occupancy.occupant(code)
        if ship is not None:
            runtime.fleet.add(ship).moor(code)
            docked[ship] = code
    for identifier in runtime.fleet.identifiers():
        if identifier not in docked and identifier in runtime.fleet.moored():
            runtime.fleet.undock(identifier)


# --- bookings --------------------------------------------------------------


def booking_create(runtime, request: Request) -> Response:
    payload = request.json_object()
    grace = optional_number(payload, "grace_minutes", 15.0)
    if grace < 0:
        raise BadRequest("grace_minutes must be non-negative")
    decision = runtime.arbiter.book(
        ship=require_text(payload, "ship"),
        berth=require_text(payload, "berth"),
        start=require_text(payload, "start"),
        end=require_text(payload, "end"),
        grace_minutes=int(grace),
    )
    booking = runtime.arbitration.bookings.get(decision.booking_id)
    body = _decision_payload(decision)
    body["booking"] = booking.as_dict() if booking else None
    return Response.json(200, body)


def booking_list(runtime, request: Request) -> Response:
    ship = request.query_one("ship")
    berth = request.query_one("berth")
    state = request.query_one("state")
    bookings = runtime.arbitration.bookings.all()
    if ship:
        bookings = [item for item in bookings if item.ship == ship]
    if berth:
        bookings = [item for item in bookings if item.berth == berth]
    if state:
        bookings = [item for item in bookings if item.state.value == state]
    return Response.json(
        200,
        {
            "count": len(bookings),
            "bookings": [item.as_dict() for item in bookings],
            "rebookable_states": sorted(item.value for item in BOOKING_REBOOKABLE),
        },
    )


def booking_detail(runtime, request: Request) -> Response:
    booking_id = require_query(request, "booking")
    booking = runtime.arbitration.bookings.get(booking_id)
    if booking is None:
        raise BookingNotFound(f"unknown booking {booking_id}")
    return Response.json(200, {"booking": booking.as_dict()})


def booking_arrive(runtime, request: Request) -> Response:
    payload = request.json_object()
    booking_id = require_text(payload, "booking")
    decision = runtime.arbiter.arrive(booking_id)
    reconcile_fleet(runtime)
    return Response.json(200, _decision_payload(decision))


def booking_rebook(runtime, request: Request) -> Response:
    payload = request.json_object()
    decision = runtime.arbiter.rebook(
        booking_id=require_text(payload, "booking"),
        berth=optional_text(payload, "berth") or None,
        start=optional_text(payload, "start") or None,
        end=optional_text(payload, "end") or None,
    )
    booking = runtime.arbitration.bookings.get(decision.booking_id)
    body = _decision_payload(decision)
    body["booking"] = booking.as_dict() if booking else None
    return Response.json(200, body)


def booking_cancel(runtime, request: Request) -> Response:
    payload = request.json_object() if request.body else {}
    booking_id = require_text(payload, "booking") if payload else request.query_one("booking", "")
    if not booking_id:
        raise BadRequest("booking is required")
    decision = runtime.arbiter.cancel(booking_id)
    return Response.json(200, _decision_payload(decision))


# --- walk-in traffic -------------------------------------------------------


def walk_in(runtime, request: Request) -> Response:
    payload = request.json_object()
    ship = require_text(payload, "ship")
    stay_end = optional_text(payload, "stay_end") or None
    preferred = optional_text(payload, "berth")
    enqueue = bool(payload.get("queue", True))
    decision = runtime.arbiter.walk_in(
        ship=ship,
        stay_end=stay_end,
        preferred_berth=preferred,
        enqueue_if_blocked=enqueue,
    )
    reconcile_fleet(runtime)
    return Response.json(200, _decision_payload(decision))


def queue_list(runtime, request: Request) -> Response:
    waiters = runtime.arbitration.queue.all()
    return Response.json(
        200,
        {"count": len(waiters), "queue": [item.as_dict() for item in waiters]},
    )


def queue_cancel(runtime, request: Request) -> Response:
    payload = request.json_object()
    decision = runtime.arbiter.cancel_wait(require_text(payload, "waiter"))
    return Response.json(200, _decision_payload(decision))


# --- timeout sweep / departure --------------------------------------------


def sweep(runtime, request: Request) -> Response:
    decisions = runtime.arbiter.sweep_timeouts()
    reconcile_fleet(runtime)
    return Response.json(
        200,
        {
            "count": len(decisions),
            "decisions": [item.as_dict() for item in decisions],
        },
    )


def vacate(runtime, request: Request) -> Response:
    payload = request.json_object()
    berth = require_text(payload, "berth")
    decision = runtime.arbiter.vacate(berth)
    reconcile_fleet(runtime)
    return Response.json(200, _decision_payload(decision))


# --- outbox ----------------------------------------------------------------


def notices(runtime, request: Request) -> Response:
    ship = request.query_one("ship")
    pending_only = request.query_one("pending") == "1"
    items = (
        runtime.outbox.pending(ship or None)
        if pending_only
        else runtime.outbox.list(ship or None)
    )
    return Response.json(
        200,
        {"count": len(items), "notices": [item.as_dict() for item in items]},
    )


def notice_deliver(runtime, request: Request) -> Response:
    payload = request.json_object()
    notice_id = require_text(payload, "notice")
    marked = runtime.outbox.mark_delivered(notice_id)
    if not marked:
        raise BookingNotFound(f"unknown notice {notice_id}")
    return Response.json(200, {"delivered": True, "notice": notice_id})


# --- helpers ---------------------------------------------------------------


def require_query(request: Request, name: str) -> str:
    value = request.query_one(name)
    if not value:
        raise BadRequest(f"{name} query parameter is required")
    return value
