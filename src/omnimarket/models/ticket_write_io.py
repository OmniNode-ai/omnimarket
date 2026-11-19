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
                field: getattr(self, field)
                for field in ModelCreateTicketRequest.model_fields
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


class ModelTicketContractWritten(BaseModel):
    """Checked Linear readback. Consumers deduplicate on the payload's event_id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    draft_id: UUID
    ticket_id: str = Field(min_length=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")
    content_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="SHA256 of the UTF-8 compact JSON array [title, description].",
    )
    updated_at: str = Field(min_length=1, description="Linear's unmodified updatedAt.")
