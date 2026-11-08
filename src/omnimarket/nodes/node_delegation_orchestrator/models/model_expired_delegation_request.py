# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed evidence for a delegation request that arrived past its deadline (OMN-22822).

A delegation request names the moment its caller asked (``emitted_at``). The
contract-declared completion bound (``completion_bound.max_wall_seconds``) after
that moment is the request's deadline: the caller stopped waiting then. A
dead-letter replay that re-delivers the request later than that is not a new
ask. Routing it opens a provider attempt nobody will read, so the orchestrator
records it with this model on
``DelegationWorkflowState.expired_delegation_request``, closes the run FAILED
with a timeout outcome, and emits no routing intent.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelExpiredDelegationRequest(BaseModel):
    """Evidence record for one delegation request refused as expired."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Workflow correlation the expired request addressed."
    )
    task_type: str = Field(..., description="Task type the expired request named.")
    emitted_at: datetime = Field(
        ..., description="When the caller emitted the request (its own clock)."
    )
    elapsed_seconds: int = Field(
        ...,
        ge=0,
        description="Wall seconds from the request's emission to its arrival here.",
    )
    completion_bound_seconds: int = Field(
        ...,
        gt=0,
        description="The contract-declared completion bound the elapsed time exceeded.",
    )
    expired_at: str = Field(
        ..., description="ISO-8601 UTC time the orchestrator refused the request."
    )


__all__: list[str] = ["ModelExpiredDelegationRequest"]
