# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, result and settings of lane intake's ticket create (OMN-23138).

Lane intake opens a ticket by publishing the ticket write node's create command and reading
the terminal that names it. These are the shapes lane intake sees: it never touches the ticket
node's handler, so a refusal on the far side reaches the caller as ``ModelLaneIntakeCreateFailed``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumLaneIntakeFailure(StrEnum):
    """Why a create did not produce a ticket.

    The first four are the reasons the ticket write node itself terminates with; the rest are
    the ways the answer can fail to arrive or to be read.
    """

    GUARD_REFUSED = "guard_refused"
    GUARD_UNAVAILABLE = "guard_unavailable"
    SECRET_UNAVAILABLE = "secret_unavailable"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    OPERATION_FAILED = "operation_failed"
    NO_TERMINAL = "no_terminal"
    BAD_TERMINAL = "bad_terminal"
    NO_TICKET_ID = "no_ticket_id"


class ModelLaneIntakeCreateSettings(BaseModel):
    """Settings of the create path, read from the overlay; none has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_s: float = Field(
        gt=0, description="Seconds to wait for the terminal event."
    )


class ModelLaneIntakeCreateRequest(BaseModel):
    """One ticket lane intake wants opened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1)
    description: str
    requested_by_lane: str = Field(min_length=1)
    requested_by_run: str | None = None
    parent: str | None = None
    repo: str | None = None
    team: str | None = None
    pillar: str | None = None
    idempotency_key: str | None = Field(
        default=None,
        description="Caller's key; absent, it is derived from the request's own content.",
    )


class ModelLaneIntakeCreated(BaseModel):
    """The ticket exists: the typed completion the ticket write node returned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["created"] = "created"
    correlation_id: UUID
    ticket_id: str = Field(min_length=1)
    ticket_url: str
    replayed: bool = False


class ModelLaneIntakeCreateFailed(BaseModel):
    """No ticket was opened; ``reason`` says why and ``finding_codes`` carries the guard's own."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["failed"] = "failed"
    correlation_id: UUID
    reason: EnumLaneIntakeFailure
    finding_codes: list[str] = Field(default_factory=list)


ModelLaneIntakeCreateResult = ModelLaneIntakeCreated | ModelLaneIntakeCreateFailed

__all__ = [
    "EnumLaneIntakeFailure",
    "ModelLaneIntakeCreateFailed",
    "ModelLaneIntakeCreateRequest",
    "ModelLaneIntakeCreateResult",
    "ModelLaneIntakeCreateSettings",
    "ModelLaneIntakeCreated",
]
