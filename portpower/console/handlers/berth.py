"""Berth inventory, occupancy and arbitration endpoints."""

from __future__ import annotations

from typing import Any

from portpower.berth.decisions import Decision, Outcome
from portpower.berth.rules import ALL as RULE_TABLE
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
    """Legacy walk-in entry point; routed through the shared arbiter."""

    payload = request.json_object()
    decisions = runtime.arbiter.arrive(
        require_text(payload, "ship"),
        berth=optional_text(payload, "berth") or None,
        stay_end=optional_text(payload, "stay_end") or None,
        preempt=_flag(payload, "preempt"),
        reason=optional_text(payload, "reason"),
        moment=optional_text(payload, "now") or None,
    )
    return Response.json(200, _arrival_payload(runtime, decisions))


def arrive(runtime, request: Request) -> Response:
    """Unified arrival: reservation check-in or walk-in placement."""

    payload = request.json_object()
    decisions = runtime.arbiter.arrive(
        require_text(payload, "ship"),
        berth=optional_text(payload, "berth") or None,
        booking=optional_text(payload, "booking") or None,
        stay_end=optional_text(payload, "stay_end") or None,
        preempt=_flag(payload, "preempt"),
        reason=optional_text(payload, "reason"),
        moment=optional_text(payload, "now") or None,
    )
    return Response.json(200, _arrival_payload(runtime, decisions))


def release_berth(runtime, request: Request) -> Response:
    payload = request.json_object()
    code = require_text(payload, "berth")
    decisions = runtime.arbiter.vacate(
        code, moment=optional_text(payload, "now") or None
    )
    return Response.json(
        200,
        {
            "vacated": bool(decisions),
            "decisions": [item.as_dict() for item in decisions],
        },
    )


def sweep(runtime, request: Request) -> Response:
    payload = request.json_object()
    decisions = runtime.arbiter.sweep(moment=optional_text(payload, "now") or None)
    return Response.json(
        200,
        {
            "count": len(decisions),
            "decisions": [item.as_dict() for item in decisions],
        },
    )


def berth_decisions(runtime, request: Request) -> Response:
    entries = runtime.arbiter.decisions(
        berth=request.query_one("berth"),
        ship=request.query_one("ship"),
        rule=request.query_one("rule"),
        booking=request.query_one("booking"),
    )
    return Response.json(
        200,
        {"count": len(entries), "decisions": [item.as_dict() for item in entries]},
    )


def berth_queue(runtime, request: Request) -> Response:
    entries = runtime.arbiter.queue()
    return Response.json(
        200,
        {
            "count": len(entries),
            "queue": [
                dict(item.as_dict(), position=index)
                for index, item in enumerate(entries, start=1)
            ],
        },
    )


def berth_rules(runtime, request: Request) -> Response:
    return Response.json(200, {"rules": [rule.as_dict() for rule in RULE_TABLE]})


def _arrival_payload(runtime, decisions: list[Decision]) -> dict[str, Any]:
    headline = decisions[-1]
    payload: dict[str, Any] = {
        "outcome": headline.outcome,
        "decisions": [item.as_dict() for item in decisions],
    }
    if headline.outcome == Outcome.ASSIGNED.value:
        payload["berth"] = headline.berth
    if headline.outcome == Outcome.QUEUED.value and headline.ticket:
        payload["queued"] = True
        payload["ticket"] = headline.ticket
        payload["position"] = runtime.arbiter.queue_position(headline.ticket)
    if headline.displaced:
        payload["displaced"] = headline.displaced
    return payload


def _flag(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)
