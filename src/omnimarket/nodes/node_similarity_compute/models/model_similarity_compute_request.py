# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for vector similarity compute operations.

Migrated from omnimemory (OMN-8297, Wave 1).
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["ModelSimilarityComputeRequest"]


class ModelSimilarityComputeRequest(BaseModel):
    """Request envelope for similarity compute operations.

    Attributes:
        operation: The similarity operation to perform.
        vector_a: First vector for comparison. Must have at least one dimension.
        vector_b: Second vector for comparison. Must match vector_a dimensions.
        metric: Distance metric for 'compare' operation. Defaults to "cosine".
        threshold: Optional threshold for is_match determination.
    """

    model_config = ConfigDict(extra="forbid")

    operation: Literal["cosine_distance", "euclidean_distance", "compare"] = Field(
        ...,
        description="The operation to perform",
    )

    vector_a: list[float] = Field(
        ...,
        min_length=1,
        description="First vector for comparison",
    )

    vector_b: list[float] = Field(
        ...,
        min_length=1,
        description="Second vector for comparison",
    )

    metric: Literal["cosine", "euclidean"] = Field(
        default="cosine",
        description="Distance metric (only used for 'compare' operation)",
    )

    threshold: float | None = Field(
        default=None,
        ge=0.0,
        description="Optional threshold for is_match determination",
    )

    @model_validator(mode="after")
    def validate_vectors_match(self) -> Self:
        """Ensure vectors have matching dimensions."""
        if len(self.vector_a) != len(self.vector_b):
            msg = f"Dimension mismatch: {len(self.vector_a)} vs {len(self.vector_b)}"
            raise ValueError(msg)
        return self
