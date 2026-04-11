# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Configuration model for the similarity compute handler.

Migrated from omnimemory (OMN-8297, Wave 1).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ModelHandlerSimilarityComputeConfig",
]


class ModelHandlerSimilarityComputeConfig(BaseModel):
    """Configuration for the similarity compute handler.

    Intentionally minimal — pure computation with no external dependencies.

    Attributes:
        epsilon: Small value for floating-point comparisons to avoid
            division by zero. Defaults to 1e-10.
    """

    model_config = ConfigDict(extra="forbid")

    epsilon: float = Field(
        default=1e-10,
        gt=0,
        description="Small value for floating-point zero comparisons",
    )
