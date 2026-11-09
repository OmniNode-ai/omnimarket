# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane intake opens a ticket by command and typed result (OMN-23138).

The rebuild plan's section 4.1 cuts dispatch from tickets-plans: lane intake no longer reaches
into the ticket node's handler. It publishes the ticket write node's create command, waits for
the terminal that names it and returns a typed result, either the ticket or a typed failure.
The bus is an injected caller (``ScmBusCaller`` in production, an in-memory one in tests); the
topics come from that node's contract and the wait from the overlay's settings.

This module imports no ticket node handler. The topic list and the command model are the
command contract and are the only things shared with the ticket write node.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError

from omnimarket.models.model_lane_intake_create import (
    EnumLaneIntakeFailure,
    ModelLaneIntakeCreated,
    ModelLaneIntakeCreateFailed,
    ModelLaneIntakeCreateRequest,
    ModelLaneIntakeCreateResult,
    ModelLaneIntakeCreateSettings,
)
from omnimarket.models.scm import ModelScmTerminal, ModelScmTopics
from omnimarket.nodes.node_linear_ticket_write_effect.models.model_ticket_write_io import (
    ModelTicketWriteCompleted,
    ModelTicketWriteFailed,
    ModelTicketWriteRequest,
)

_KNOWN_REASONS = {reason.value: reason for reason in EnumLaneIntakeFailure}


class ProtocolTicketCommandCaller(Protocol):
    """Publishes one command and returns the terminal naming it (``ScmBusCaller``'s shape)."""

    async def call_topics(
        self,
        *,
        topics: ModelScmTopics,
        request: BaseModel,
        correlation_id: UUID,
        timeout_s: float,
    ) -> ModelScmTerminal: ...


def derive_idempotency_key(request: ModelLaneIntakeCreateRequest) -> str:
    """The caller's key, else a digest of the request's content so a retry replays."""
    if request.idempotency_key:
        return request.idempotency_key
    body = request.model_dump(mode="json", exclude={"idempotency_key"})
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class HandlerLaneIntakeCreate:
    """Create a ticket for a lane by publishing the create command; read the typed terminal."""

    def __init__(
        self,
        *,
        caller: ProtocolTicketCommandCaller,
        topics: ModelScmTopics,
        settings: ModelLaneIntakeCreateSettings,
    ) -> None:
        self._caller = caller
        self._topics = topics
        self._settings = settings

    async def handle(
        self, request: ModelLaneIntakeCreateRequest
    ) -> ModelLaneIntakeCreateResult:
        correlation_id = uuid4()
        fields = {
            name: value
            for name, value in request.model_dump(exclude={"idempotency_key"}).items()
            if value is not None
        }
        command = ModelTicketWriteRequest.model_validate(
            {
                **fields,
                "operation": "create",
                "idempotency_key": derive_idempotency_key(request),
                "correlation_id": correlation_id,
            }
        )
        try:
            terminal = await self._caller.call_topics(
                topics=self._topics,
                request=command,
                correlation_id=correlation_id,
                timeout_s=self._settings.timeout_s,
            )
        except TimeoutError:
            return self._failed(correlation_id, EnumLaneIntakeFailure.NO_TERMINAL)
        return self._read(correlation_id, terminal)

    def _read(
        self, correlation_id: UUID, terminal: ModelScmTerminal
    ) -> ModelLaneIntakeCreateResult:
        try:
            if terminal.failed:
                refusal = ModelTicketWriteFailed.model_validate(terminal.payload)
                return self._failed(
                    correlation_id,
                    _KNOWN_REASONS.get(
                        refusal.reason, EnumLaneIntakeFailure.OPERATION_FAILED
                    ),
                    refusal.finding_codes,
                )
            done = ModelTicketWriteCompleted.model_validate(terminal.payload)
        except ValidationError:
            return self._failed(correlation_id, EnumLaneIntakeFailure.BAD_TERMINAL)
        if not done.result.ticket_id:
            return self._failed(correlation_id, EnumLaneIntakeFailure.NO_TICKET_ID)
        return ModelLaneIntakeCreated(
            correlation_id=correlation_id,
            ticket_id=done.result.ticket_id,
            ticket_url=done.result.ticket_url,
            replayed=done.replayed,
        )

    @staticmethod
    def _failed(
        correlation_id: UUID,
        reason: EnumLaneIntakeFailure,
        finding_codes: list[str] | None = None,
    ) -> ModelLaneIntakeCreateResult:
        return ModelLaneIntakeCreateFailed(
            correlation_id=correlation_id,
            reason=reason,
            finding_codes=list(finding_codes or []),
        )


__all__ = [
    "HandlerLaneIntakeCreate",
    "ProtocolTicketCommandCaller",
    "derive_idempotency_key",
]
