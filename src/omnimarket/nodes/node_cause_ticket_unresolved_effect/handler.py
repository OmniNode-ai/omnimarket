# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Detect and resolve the cause-ticket-unresolved exception class in code (OMN-22143).

The merge-sweep dispatcher emits ``cause-ticket-unresolved`` when it plans a lane it cannot place under a ticket:
it tried the red PR tickets, the exact red commit subjects, an explicit diagnose ticket and the lane_dispatch
overlay's ``standing_tickets``, with the umbrella excluded. A floor-only diagnose (reasons all ``under floor: ...``,
no reds) cites no ticket of its own, so only the overlay's standing ticket can place it.

* ``classify_block`` recognises the class from the block's key and what it tried, not from its class label.
* ``HandlerCauseTicketUnresolved`` resolves a case when the overlay now names an open, non-umbrella standing
  ticket for the lane kind, and emits to the exception queue only a case it cannot resolve. A decision row is
  never resolved.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_cause_ticket_unresolved_effect.container.container_cause_ticket_unresolved_effect import (
    build_handler_exception_emit,
    build_handler_exception_resolve,
)
from omnimarket.nodes.node_cause_ticket_unresolved_effect.models import (
    ModelCauseTicketCase,
    ModelCauseTicketResolution,
    ModelCauseTicketUnresolvedRequest,
    ModelCauseTicketUnresolvedResult,
)
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionEmitRequest,
    ModelExceptionResolveRequest,
    ModelExceptionRow,
)

CLASS = "cause-ticket-unresolved"
SOURCE = "node_cause_ticket_unresolved_effect"
OVERLAY_TRIED = "lane_dispatch overlay standing_tickets"
CLOSED_STATES = frozenset({"done", "canceled", "cancelled", "duplicate"})

_KEY = re.compile(r"^merge-sweep-(?P<kind>[a-z][a-z0-9-]*):(?P<body>\{.*\})$")
_TICKET = re.compile(r"OMN-[0-9]+")
_FLOOR = "under floor:"


def classify_block(row: Mapping[str, object]) -> ModelCauseTicketCase | None:
    """The cause-ticket-unresolved case a block holds, or None for any other block.

    The block is recognised by its merge-sweep lane key and by having tried the overlay's standing tickets.
    """
    key = str(row.get("key", ""))
    match = _KEY.match(key)
    if match is None or OVERLAY_TRIED not in str(row.get("tried", "")):
        return None
    try:
        body = json.loads(match["body"])
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    reasons = tuple(str(r) for r in body.get("reasons") or ())
    reds = tuple(str(r) for r in body.get("reds") or ())
    repo = body.get("repo")
    kind = match["kind"]
    return ModelCauseTicketCase(
        id=str(row.get("id", "")),
        key=key,
        lane_kind=kind,
        overlay_key=f"merge-sweep-{kind}",
        reasons=reasons,
        reds=reds,
        repo=str(repo) if repo else None,
        floor_only=bool(reasons)
        and not reds
        and all(r.startswith(_FLOOR) for r in reasons),
        decision=bool(row.get("decision")),
    )


def read_standing_tickets(overlay_path: Path) -> dict[str, str]:
    """The ``standing_tickets`` map of a lane_dispatch overlay file; an absent file or key is an empty map."""
    if not overlay_path.is_file():
        return {}
    doc: Any = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    standing = doc.get("standing_tickets") if isinstance(doc, dict) else None
    if not isinstance(standing, dict):
        return {}
    return {str(k): str(v) for k, v in standing.items() if isinstance(v, str)}


def standing_ticket(
    case: ModelCauseTicketCase, request: ModelCauseTicketUnresolvedRequest
) -> tuple[str, str]:
    """(ticket, '') when the overlay names an open, non-umbrella ticket for the lane kind, else ('', why not)."""
    where = f"standing_tickets[{case.overlay_key!r}]"
    ticket = request.standing_tickets.get(case.overlay_key, "").strip()
    if not ticket:
        return "", f"read the lane_dispatch overlay: no {where}"
    if not _TICKET.fullmatch(ticket):
        return (
            "",
            f"read the lane_dispatch overlay: {where} is {ticket!r}, not a ticket id",
        )
    if ticket in request.umbrella_tickets:
        return (
            "",
            f"read the lane_dispatch overlay: {where} names the umbrella {ticket}",
        )
    state = request.ticket_states.get(ticket, "").strip()
    if not state:
        return (
            "",
            f"read the lane_dispatch overlay: {where} is {ticket}, whose state is unread",
        )
    if state.lower() in CLOSED_STATES:
        return (
            "",
            f"read the lane_dispatch overlay: {where} is {ticket}, which is {state}",
        )
    return ticket, ""


class HandlerCauseTicketUnresolved:
    """Resolve each cause-ticket-unresolved row in code, or emit the case to the exception queue once."""

    def handle(
        self, request: ModelCauseTicketUnresolvedRequest
    ) -> ModelCauseTicketUnresolvedResult:
        return ModelCauseTicketUnresolvedResult(
            resolutions=tuple(self._settle(row, request) for row in request.rows)
        )

    def _settle(
        self, row: Mapping[str, Any], request: ModelCauseTicketUnresolvedRequest
    ) -> ModelCauseTicketResolution:
        row_id = str(row.get("id", ""))
        case = classify_block(row)
        if case is None:
            return ModelCauseTicketResolution(
                id=row_id,
                cls="",
                resolved=False,
                ticket="",
                how=f"not a {CLASS} block",
                emitted="",
            )
        if case.decision:
            return ModelCauseTicketResolution(
                id=row_id,
                cls=CLASS,
                resolved=False,
                ticket="",
                how="decision row: left to the operator",
                emitted="",
            )
        queue_dir = request.state_dir / "exception-queue"
        ticket, why_not = standing_ticket(case, request)
        if ticket:
            state = request.ticket_states[ticket]
            how = (
                f"the lane_dispatch overlay names standing_tickets[{case.overlay_key!r}] = {ticket} "
                f"({state}), open and not an umbrella: the merge sweep plans this {case.lane_kind} lane under it"
            )
            if row_id:
                build_handler_exception_resolve().handle(
                    ModelExceptionResolveRequest(
                        queue_dir, row_id, SOURCE, how, request.now
                    )
                )
            return ModelCauseTicketResolution(
                id=row_id, cls=CLASS, resolved=True, ticket=ticket, how=how, emitted=""
            )
        emitted = build_handler_exception_emit().handle(
            ModelExceptionEmitRequest(
                state_dir=queue_dir,
                row=ModelExceptionRow(
                    cls=CLASS,
                    key=case.key,
                    source=SOURCE,
                    evidence=str(row.get("evidence", "")) or case.key,
                    tried=why_not,
                    proposed_fix=(
                        f"name an open child ticket (never the umbrella) under "
                        f"standing_tickets: {{{case.overlay_key}: OMN-<n>}} in the lane_dispatch overlay"
                    ),
                    decision=False,
                    repo=case.repo or "",
                ),
                now=request.now,
            )
        )
        return ModelCauseTicketResolution(
            id=row_id,
            cls=CLASS,
            resolved=False,
            ticket="",
            how=why_not,
            emitted=f"{emitted.status}:{emitted.id}",
        )
