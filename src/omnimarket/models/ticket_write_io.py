# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed ticket bus commands and terminal results (OMN-20595)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)


class ModelTicketWriteRequest(ModelCreateTicketRequest):
    """The guarded operation plus the same caller/replay identity as SCM."""

    correlation_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=256)
    requested_by_lane: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    requested_by_run: str | None = Field(default=None, min_length=1, max_length=256)

    def ticket_request(self) -> ModelCreateTicketRequest:
        return ModelCreateTicketRequest.model_validate(
            {
                key: value
                for key, value in self.model_dump().items()
                if key in ModelCreateTicketRequest.model_fields
            }
        )


class _TicketTerminal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    operation: EnumTicketOperation
    idempotency_key: str
    requested_by_lane: str
    requested_by_run: str | None = None


class ModelTicketWriteCompleted(_TicketTerminal):
    outcome: Literal["completed"] = "completed"
    replayed: bool = False
    result: ModelCreateTicketResult


class ModelTicketWriteFailed(_TicketTerminal):
    outcome: Literal["failed"] = "failed"
    reason: str
    terminal_failure_cause: str
    finding_codes: list[str] = Field(default_factory=list)
