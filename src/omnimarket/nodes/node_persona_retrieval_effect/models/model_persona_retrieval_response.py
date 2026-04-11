# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Response model for persona retrieval."""

from typing import Literal

from omnimemory.models.persona import ModelUserPersonaV1
from pydantic import BaseModel, ConfigDict, Field


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
