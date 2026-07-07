# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Response model for persona lifecycle orchestration.

OMN-14010: was a "compatibility import" of
omnimemory.nodes.node_persona_lifecycle_orchestrator.models.model_persona_lifecycle_response,
which never shipped and was deleted as dead scaffolding (OMN-12172, "models
only, empty handlers/, OMN-7305 never shipped"). Recovered from git history
(omnimemory@72ddef2~1) and inlined here so this node is self-contained.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelPersonaLifecycleResponse"]


class ModelPersonaLifecycleResponse(BaseModel):
    """Response from persona lifecycle orchestration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["success", "error"] = Field(
        ...,
        description="Orchestration outcome",
    )
    users_processed: int = Field(
        default=0,
        ge=0,
        description="Number of users whose personas were rebuilt",
    )
    personas_created: int = Field(
        default=0,
        ge=0,
        description="Number of new persona snapshots stored",
    )
    users_skipped: int = Field(
        default=0,
        ge=0,
        description="Users skipped due to insufficient data",
    )
    error_message: str | None = Field(
        default=None,
        description="Error description when status is 'error'",
    )
