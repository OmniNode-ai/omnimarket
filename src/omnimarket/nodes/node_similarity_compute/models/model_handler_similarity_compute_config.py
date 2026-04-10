# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Configuration model for the similarity compute handler.

HandlerSimilarityCompute.

.. versionadded:: 0.1.0
    Initial implementation for OMN-1388.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ModelHandlerSimilarityComputeConfig",
]


class ModelHandlerSimilarityComputeConfig(BaseModel):
    """Configuration for the similarity compute handler.

    This is intentionally minimal as the handler performs pure computation
    with no external dependencies or configurable behavior.

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
