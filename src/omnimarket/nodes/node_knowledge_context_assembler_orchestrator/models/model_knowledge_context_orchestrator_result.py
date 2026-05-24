# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Output model for the knowledge context assembler orchestrator."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.knowledge_context import (
    ModelKnowledgeContextBundle,
)

__all__ = [
    "ModelKnowledgeContextOrchestratorResult",
]

OrchestratorStatus = Literal["COMPLETE", "PARTIAL", "DEGRADED"]


class ModelKnowledgeContextOrchestratorResult(BaseModel):
    """Result emitted by the orchestrator after fan-out + reduction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., description="Echoed correlation ID")
    status: OrchestratorStatus = Field(
        ...,
        description="COMPLETE=all backends ok, PARTIAL=some failed, DEGRADED=all failed",
    )
    bundle: ModelKnowledgeContextBundle = Field(
        ..., description="Assembled knowledge context bundle"
    )
    succeeded_backend_count: int = Field(
        ..., description="Number of backends that returned successfully"
    )
    failed_backend_count: int = Field(
        ..., description="Number of backends that failed or timed out"
    )
