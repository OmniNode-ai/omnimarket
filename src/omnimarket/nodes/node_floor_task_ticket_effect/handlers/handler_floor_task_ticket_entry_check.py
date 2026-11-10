# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the entry ticket stored for one floor task and say whether a lane may be started on it.

One ``read`` under ``floor_fill.mint_timeout_seconds``. The ticket must carry the task's Floor key line, be open, not be
an umbrella and not be visible to the old fill; a read that fails, runs past the deadline or finds no ticket is
``unreadable``.
"""

from __future__ import annotations

from omnibase_spi.contracts.services import ModelFloorTicketLookupRequest
from omnibase_spi.protocols.lane import ProtocolFloorTicketStore

from omnimarket.nodes.node_floor_task_ticket_effect.handlers.bounded_call import (
    CallDeadlineError,
    CallFailedError,
    bounded,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers.ticket_judge import (
    judge,
)
from omnimarket.nodes.node_floor_task_ticket_effect.models import (
    ModelFloorTaskEntryTicketRequest,
    ModelFloorTaskEntryTicketVerdict,
)

KEY_PREFIX = "floor:"


class HandlerFloorTaskTicketEntryCheck:
    """Judge the entry ticket of one task; the ticket store is injected."""

    def __init__(self, store: ProtocolFloorTicketStore) -> None:
        self._store = store

    def handle(
        self, request: ModelFloorTaskEntryTicketRequest
    ) -> ModelFloorTaskEntryTicketVerdict:
        def unreadable(cause: str) -> ModelFloorTaskEntryTicketVerdict:
            return ModelFloorTaskEntryTicketVerdict(
                line_id=request.line_id,
                task_id=request.task_id,
                status="unreadable",
                cause=cause,
            )

        try:
            found = bounded(
                self._store.read,
                ModelFloorTicketLookupRequest(ticket=request.ticket),
                request.mint_timeout_seconds,
            )
        except CallDeadlineError:
            return unreadable("read-timeout")
        except CallFailedError as failed:
            return unreadable(f"read-failed:{failed.cause}")
        if found.state != "found":
            return unreadable(
                "ticket-missing"
                if found.state == "missing"
                else f"read-failed:{found.cause}"
            )
        key = f"{KEY_PREFIX}{request.line_id}:{request.task_id}"
        verdict = judge(found, key)
        if verdict == "usable":
            return ModelFloorTaskEntryTicketVerdict(
                line_id=request.line_id,
                task_id=request.task_id,
                status="usable",
                ticket=request.ticket,
            )
        if verdict == "wrong-key":
            return unreadable("no-floor-key-line")
        return ModelFloorTaskEntryTicketVerdict(
            line_id=request.line_id,
            task_id=request.task_id,
            status="refused",
            refusal=verdict,
        )
