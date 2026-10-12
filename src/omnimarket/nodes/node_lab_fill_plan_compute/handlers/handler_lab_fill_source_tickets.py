# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Name the tickets the stalled-plan and pending-ruling sources point at (OMN-20668).

plan-stalled is the next-action ticket of each STALLED plan whose priority and next-action
kind are configured; ruling-pending is each pending mechanism ticket of an unmechanized
ruling. A source whose document has no list, or holds a value that cannot be read, is
unreadable, never empty, and its tickets are not used.
"""

import re
from collections import Counter
from pathlib import Path

from pydantic import JsonValue

from ..models.model_lab_fill_sources import (
    ModelLabFillSourceDiagnostic,
    ModelLabFillSourceRead,
    ModelLabFillSourceTicket,
    ModelLabFillSourceTicketsRequest,
    ModelLabFillSourceTicketsResult,
)

SOURCE_KINDS = ("plan-stalled", "ruling-pending")
TICKET_RE = re.compile("OMN-[0-9]+")


class LabFillSourceUnreadableError(Exception):
    """A source document the old reader would have refused."""


def source_error(detail: object) -> str:
    """One line of at most 100 characters, as the STATUS cell carries it."""
    return " ".join(str(detail).split())[:100]


def _list_of(document: JsonValue, key: str, label: str) -> list[JsonValue]:
    found = document.get(key) if isinstance(document, dict) else None
    if not isinstance(found, list):
        raise LabFillSourceUnreadableError(label)
    return found


def _plan_tickets(
    document: JsonValue,
    request: ModelLabFillSourceTicketsRequest,
    drops: Counter[str],
) -> list[tuple[str, str]]:
    found = []
    for plan in _list_of(document, "plans", "plans-today printed no plans list"):
        if not isinstance(plan, dict) or plan.get("verdict") != "STALLED":
            continue
        ticket = plan.get("next_action_ticket")
        if plan.get("priority") not in request.plan_priorities:
            drops["not-priority"] += 1
        elif plan.get("next_action_kind") not in request.plan_action_kinds:
            drops["not-lane-work"] += 1
        elif not (isinstance(ticket, str) and TICKET_RE.fullmatch(ticket)):
            drops["no-ticket"] += 1
        else:
            found.append(
                (
                    ticket,
                    "plan "
                    + Path(str(plan.get("path", "plan"))).name
                    + ", priority "
                    + str(plan["priority"])
                    + ", idle "
                    + str(plan.get("hours_idle"))
                    + " h",
                )
            )
    return found


def _iterable(value: JsonValue) -> list[JsonValue]:
    """What a for loop over the value visits; a number cannot be read as tickets."""
    if not value:
        return []
    if isinstance(value, (list, str)):
        return list(value)
    if isinstance(value, dict):
        return list(value.keys())
    raise LabFillSourceUnreadableError(
        f"'{type(value).__name__}' object is not iterable"
    )


def _ruling_tickets(document: JsonValue, drops: Counter[str]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    unmechanized = _list_of(
        document, "unmechanized", "ruling-mechanisms printed no unmechanized list"
    )
    for ruling in unmechanized:
        if not isinstance(ruling, dict):
            continue
        pending = [
            t
            for t in _iterable(ruling.get("mechanism_tickets"))
            if isinstance(t, str) and TICKET_RE.fullmatch(t)
        ]
        if not pending:
            drops["no-pending-ticket"] += 1
        evidence = (
            "RULING " + str(ruling.get("ts")) + " lane=" + str(ruling.get("lane"))
        )
        found.extend((t, evidence) for t in pending)
    return found


class HandlerLabFillSourceTickets:
    """Read both tier-1 source documents into tickets, drops and per-source reads."""

    def handle(
        self, request: ModelLabFillSourceTicketsRequest
    ) -> ModelLabFillSourceTicketsResult:
        reads = dict.fromkeys(SOURCE_KINDS, "empty")
        drops: dict[str, Counter[str]] = {kind: Counter() for kind in SOURCE_KINDS}
        found: list[ModelLabFillSourceTicket] = []
        configured: tuple[tuple[str, ModelLabFillSourceRead | None], ...] = (
            ("plan-stalled", request.plans),
            ("ruling-pending", request.rulings),
        )
        for kind, read in configured:
            if read is None:
                continue
            try:
                if read.error:
                    raise LabFillSourceUnreadableError(read.error)
                tickets = (
                    _plan_tickets(read.document, request, drops[kind])
                    if kind == "plan-stalled"
                    else _ruling_tickets(read.document, drops[kind])
                )
            except LabFillSourceUnreadableError as exc:
                reads[kind] = "unreadable:" + source_error(exc)
                continue
            reads[kind] = "ok"
            found.extend(
                ModelLabFillSourceTicket(kind=kind, ticket=t, evidence=e)
                for t, e in tickets
            )
        return ModelLabFillSourceTicketsResult(
            sources={
                kind: ModelLabFillSourceDiagnostic(
                    read=reads[kind],
                    dropped=sum(drops[kind].values()),
                    reasons=dict(drops[kind]),
                )
                for kind in SOURCE_KINDS
            },
            found=tuple(found),
            wanted=tuple(sorted({f.ticket for f in found})),
        )
