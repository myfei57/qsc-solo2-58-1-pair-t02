"""Berth reservation endpoints."""

from __future__ import annotations

from portpower.console.http import Request, Response
from portpower.console.validation import optional_text, require_text


def reservation_book(runtime, request: Request) -> Response:
    payload = request.json_object()
    reservation = runtime.arbiter.book(
        ship=require_text(payload, "ship"),
        berth=require_text(payload, "berth"),
        start=require_text(payload, "start"),
        end=require_text(payload, "end"),
        moment=optional_text(payload, "now") or None,
    )
    return Response.json(
        200,
        {"booked": True, "reservation": reservation.as_dict()},
    )


def reservation_list(runtime, request: Request) -> Response:
    code = request.query_one("code")
    if code:
        reservation = runtime.scheduler.get(code)
        return Response.json(
            200,
            {
                "found": reservation is not None,
                "reservation": reservation.as_dict() if reservation else None,
            },
        )
    status = request.query_one("status")
    reservations = runtime.scheduler.list_all(status=status or None)
    berth = request.query_one("berth")
    if berth:
        reservations = [item for item in reservations if item.berth == berth]
    ship = request.query_one("ship")
    if ship:
        reservations = [item for item in reservations if item.ship == ship]
    return Response.json(
        200,
        {"count": len(reservations), "reservations": [r.as_dict() for r in reservations]},
    )


def reservation_cancel(runtime, request: Request) -> Response:
    code = request.query_one("code")
    if code:
        cancelled = [runtime.arbiter.cancel(code)]
    else:
        berth = request.query_one("berth", "") or ""
        cancelled = [
            runtime.arbiter.cancel(item.code)
            for item in runtime.scheduler.booked_on(berth)
        ]
    return Response.json(
        200,
        {
            "cancelled": len(cancelled),
            "reservations": [item.as_dict() for item in cancelled],
            "remaining": runtime.scheduler.count(),
        },
    )


def reservation_rebook(runtime, request: Request) -> Response:
    payload = request.json_object()
    reservation = runtime.arbiter.rebook(
        code=require_text(payload, "code"),
        start=require_text(payload, "start"),
        end=require_text(payload, "end"),
        berth=optional_text(payload, "berth") or None,
        moment=optional_text(payload, "now") or None,
    )
    return Response.json(
        200,
        {"rebooked": True, "reservation": reservation.as_dict()},
    )
