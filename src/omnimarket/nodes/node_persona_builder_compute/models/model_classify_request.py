# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for persona classification.

Migrated from omnimemory (OMN-8297, Wave 1).
Models reference omnimemory as external dep (omninode-memory).
"""

from __future__ import annotations

# omnimemory is a declared external dep
from omnimemory.models.persona import ModelPersonaSignal, ModelUserPersonaV1
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelPersonaClassifyRequest"]


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
