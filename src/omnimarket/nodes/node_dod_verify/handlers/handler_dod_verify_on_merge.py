# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verify the ticket a merged PR cites, at merge time (OMN-21146).

``node_dod_verify`` subscribes to the merge event as well as to its start
command. One merged PR that cites a ticket in, that ticket's verification out,
through the same :class:`HandlerDodVerify` run a start command gets, so its
verdict lands on the node's terminal topic and in the verdict projection without
waiting for the scheduled closeout sweep to reach the ticket.

A merge that cites no ticket, or a value that is not a ticket identifier, yields
None and publishes nothing. The verification run id is derived from the merge
event id, so a redelivered merge asks for the same run and the verdict
projection upserts it instead of storing a second one. The hosted audience is
the one a runtime-issued verification carries, as for a delegation's (OMN-19514):
a runtime verifier never runs local Done-gate commands.
"""

from __future__ import annotations

from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.events.github import ModelPrMergedEvent
from omnimarket.models.delegation.delegation_ticket_id import ticket_id_refusal
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    ModelDodVerifyState,
)

VERIFY_ON_MERGE_NAMESPACE = "onex:omnimarket:dod-verify-on-merge:"


class ProtocolDodVerifier(Protocol):
    """The verification a start command gets."""

    def handle(self, payload: ModelDodVerifyStartCommand) -> ModelDodVerifyState: ...


class _TypedDodVerify:
    """HandlerDodVerify's typed entry, narrowed to the typed result."""

    def __init__(self) -> None:
        self._handler = HandlerDodVerify()

    def handle(self, payload: ModelDodVerifyStartCommand) -> ModelDodVerifyState:
        state = self._handler.handle(payload)
        if not isinstance(state, ModelDodVerifyState):
            raise TypeError(f"dod_verify returned {type(state).__name__}")
        return state


class HandlerDodVerifyOnMerge:
    """Turn a merged PR that cites a ticket into that ticket's verification."""

    def __init__(self, *, verifier: ProtocolDodVerifier | None = None) -> None:
        self._verifier = verifier if verifier is not None else _TypedDodVerify()

    def handle(self, merged: ModelPrMergedEvent) -> ModelDodVerifyState | None:
        ticket_id = merged.ticket.strip()
        if not ticket_id or ticket_id_refusal(ticket_id) is not None:
            return None
        command = ModelDodVerifyStartCommand(
            ticket_id=ticket_id,
            correlation_id=uuid5(
                NAMESPACE_URL, f"{VERIFY_ON_MERGE_NAMESPACE}{merged.event_id}"
            ),
            execution_audience=EnumDodVerifyExecutionAudience.HOSTED,
        )
        return self._verifier.handle(command)


__all__: list[str] = [
    "VERIFY_ON_MERGE_NAMESPACE",
    "HandlerDodVerifyOnMerge",
    "ProtocolDodVerifier",
]
