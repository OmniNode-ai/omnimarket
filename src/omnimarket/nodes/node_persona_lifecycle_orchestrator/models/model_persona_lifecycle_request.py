# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for persona lifecycle orchestration.

OMN-14010: was a "compatibility import" of
omnimemory.nodes.node_persona_lifecycle_orchestrator.models.model_persona_lifecycle_request,
which never shipped and was deleted as dead scaffolding (OMN-12172, "models
only, empty handlers/, OMN-7305 never shipped"). Recovered from git history
(omnimemory@72ddef2~1) and inlined here so this node is self-contained.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelPersonaLifecycleRequest"]


class ModelPersonaLifecycleRequest(BaseModel):
    """Request for persona lifecycle operations (tick or on-demand)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["on_tick", "on_demand"] = Field(
        ...,
        description="Tick-driven fan-out or single-user on-demand rebuild",
    )
    user_id: str | None = Field(
        default=None,
        description="User ID for on-demand rebuild (required when operation='on_demand')",
    )
