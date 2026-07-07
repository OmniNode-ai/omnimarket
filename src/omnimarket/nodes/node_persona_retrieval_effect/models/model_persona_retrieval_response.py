# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Response model for persona retrieval.

OMN-14010: was a "compatibility import" of
omnimemory.nodes.node_persona_retrieval_effect.models.model_persona_retrieval_response,
which never shipped and was deleted as dead scaffolding (OMN-12172, "models
only, no handlers directory, OMN-7305 never shipped"). Recovered from git
history (omnimemory@72ddef2~1) and inlined here so this node is self-contained.
The ModelUserPersonaV1 cross-package reference is unaffected by that deletion
(still live in omnimemory.models.persona) and is kept as-is.
"""

from typing import Literal

from omnimemory.models.persona import ModelUserPersonaV1
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelPersonaRetrievalResponse"]


class ModelPersonaRetrievalResponse(BaseModel):
    """Response from persona retrieval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["found", "not_found", "error"] = Field(
        ...,
        description="Retrieval outcome",
    )
    persona: ModelUserPersonaV1 | None = Field(
        default=None,
        description="Latest persona snapshot (None if not_found or error)",
    )
    error_message: str | None = Field(
        default=None,
        description="Error description when status is 'error'",
    )
