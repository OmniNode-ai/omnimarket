# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for dispatch-worker execution effect."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dispatch_worker_execution_effect.models.model_delegation_payload import (
    ModelDispatchWorkerDelegationPayload,
)
from omnimarket.nodes.node_dispatch_worker_execution_effect.models.model_execution_outcome import (
    ModelDispatchWorkerExecutionOutcome,
)


class ModelDispatchWorkerExecutionResult(BaseModel):
    """Batch execution result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Runtime correlation ID.")
    outcomes: tuple[ModelDispatchWorkerExecutionOutcome, ...] = Field(default=())
    total_delegated: int = Field(default=0, ge=0)
    total_failed: int = Field(default=0, ge=0)
    total_rejected: int = Field(default=0, ge=0)
    total_skipped: int = Field(default=0, ge=0)
    delegation_payloads: tuple[ModelDispatchWorkerDelegationPayload, ...] = Field(
        default=(),
        description="Delegation requests for the orchestrator/runtime to publish.",
    )


__all__ = ["ModelDispatchWorkerExecutionResult"]
