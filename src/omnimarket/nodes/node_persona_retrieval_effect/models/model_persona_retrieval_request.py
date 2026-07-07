# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for persona retrieval.

OMN-14010: was a "compatibility import" of
omnimemory.nodes.node_persona_retrieval_effect.models.model_persona_retrieval_request,
which never shipped and was deleted as dead scaffolding (OMN-12172, "models
only, no handlers directory, OMN-7305 never shipped"). Recovered from git
history (omnimemory@72ddef2~1) and inlined here so this node is self-contained.
"""

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelPersonaRetrievalRequest"]


class ModelPersonaRetrievalRequest(BaseModel):
    """Request to retrieve the latest persona snapshot for a user."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(..., description="User identifier to retrieve persona for")
    agent_id: str | None = Field(
        default=None,
        description="Optional agent binding filter",
    )
