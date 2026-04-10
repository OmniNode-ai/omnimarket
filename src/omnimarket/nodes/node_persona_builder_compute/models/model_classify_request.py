# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for persona classification."""

from omnimemory.models.persona import ModelPersonaSignal, ModelUserPersonaV1
from pydantic import BaseModel, ConfigDict, Field


class ModelPersonaClassifyRequest(BaseModel):
    """Request to classify persona signals into an updated profile."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(..., description="User identifier for the persona")
    signals: list[ModelPersonaSignal] = Field(
        ...,
        description="Batch of persona signals to classify",
    )
    existing_profile: ModelUserPersonaV1 | None = Field(
        default=None,
        description="Previous persona snapshot for incremental update",
    )
