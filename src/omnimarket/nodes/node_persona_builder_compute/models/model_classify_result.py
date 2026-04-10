# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for persona classification.

Migrated from omnimemory (OMN-8297, Wave 1).
"""

from __future__ import annotations

from typing import Literal

from omnimemory.models.persona import ModelUserPersonaV1
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelPersonaClassifyResult"]


class ModelPersonaClassifyResult(BaseModel):
    """Result of persona classification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["success", "error", "insufficient_data"] = Field(
        ...,
        description="Classification outcome",
    )
    persona: ModelUserPersonaV1 | None = Field(
        default=None,
        description="Updated persona snapshot (None on error/insufficient_data)",
    )
    error_message: str | None = Field(
        default=None,
        description="Error description when status is 'error'",
    )
    signals_processed: int = Field(
        default=0,
        ge=0,
        description="Number of signals that contributed to this classification",
    )
