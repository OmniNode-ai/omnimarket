# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelBuildDispatchInput — input to the build dispatch effect node.

Related:
    - OMN-7582: Migrate node_build_dispatch_effect to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_build_dispatch_effect.models.model_build_target import (
    ModelBuildTarget,
)


class ModelBuildDispatchInput(BaseModel):
    """Input to the build dispatch effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    targets: tuple[ModelBuildTarget, ...] = Field(
        ..., description="Tickets to dispatch for building."
    )
    dry_run: bool = Field(default=False, description="Skip actual dispatch.")


__all__: list[str] = ["ModelBuildDispatchInput"]
