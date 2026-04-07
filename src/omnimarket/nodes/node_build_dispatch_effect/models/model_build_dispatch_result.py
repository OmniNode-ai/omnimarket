# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelBuildDispatchResult — result from the build dispatch effect node.

Related:
    - OMN-7582: Migrate node_build_dispatch_effect to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_build_dispatch_effect.models.model_build_dispatch_outcome import (
    ModelBuildDispatchOutcome,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_delegation_payload import (
    ModelDelegationPayload,
)


class ModelBuildDispatchResult(BaseModel):
    """Result from the build dispatch effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    outcomes: tuple[ModelBuildDispatchOutcome, ...] = Field(
        ..., description="Per-ticket dispatch outcomes."
    )
    total_dispatched: int = Field(
        default=0, ge=0, description="Successfully dispatched count."
    )
    total_failed: int = Field(default=0, ge=0, description="Failed dispatch count.")
    delegation_payloads: tuple[ModelDelegationPayload, ...] = Field(
        default=(),
        description="Delegation request payloads for the orchestrator to publish.",
    )


__all__: list[str] = ["ModelBuildDispatchResult"]
