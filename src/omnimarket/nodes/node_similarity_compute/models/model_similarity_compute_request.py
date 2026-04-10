# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Similarity Compute Request model for vector similarity operations.

This module defines the request envelope used by the similarity_compute node
to perform vector similarity calculations including cosine distance, euclidean
distance, and comprehensive vector comparisons.

Example:
    >>> from omnimemory.nodes.node_similarity_compute.models import (
    ...     ModelSimilarityComputeRequest,
    ... )
    >>> # Cosine distance calculation
    >>> request = ModelSimilarityComputeRequest(
    ...     operation="cosine_distance",
    ...     vector_a=[0.1, 0.2, 0.3],
    ...     vector_b=[0.4, 0.5, 0.6],
    ... )
    >>> # Full comparison with threshold
    >>> request = ModelSimilarityComputeRequest(
    ...     operation="compare",
    ...     vector_a=[0.1, 0.2, 0.3],
    ...     vector_b=[0.4, 0.5, 0.6],
    ...     metric="cosine",
    ...     threshold=0.8,
    ... )

.. versionadded:: 0.1.0
    Initial implementation for OMN-1388.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["ModelSimilarityComputeRequest"]


class ModelSimilarityComputeRequest(BaseModel):
    """Request envelope for similarity compute operations.

    This model encapsulates all parameters needed to perform vector similarity
    calculations. The operation field determines the calculation type, and
    other fields provide operation-specific parameters.

    Supported operations:
        - cosine_distance: Calculate cosine distance between two vectors
        - euclidean_distance: Calculate Euclidean (L2) distance between two vectors
        - compare: Full comparison with similarity score and optional threshold matching

    Attributes:
        operation: The similarity operation to perform.
        vector_a: First vector for comparison. Must have at least one dimension.
        vector_b: Second vector for comparison. Must match vector_a dimensions.
        metric: Distance metric for 'compare' operation. Defaults to "cosine".
        threshold: Optional threshold for is_match determination. When provided,
            the response will include whether the vectors match within this
            threshold.

    Example:
        >>> # Cosine distance
        >>> cosine_request = ModelSimilarityComputeRequest(
        ...     operation="cosine_distance",
        ...     vector_a=[1.0, 0.0, 0.0],
        ...     vector_b=[0.0, 1.0, 0.0],
        ... )
        >>>
        >>> # Euclidean distance
        >>> euclidean_request = ModelSimilarityComputeRequest(
        ...     operation="euclidean_distance",
        ...     vector_a=[0.0, 0.0],
        ...     vector_b=[3.0, 4.0],
        ... )
        >>>
        >>> # Full comparison with threshold
        >>> compare_request = ModelSimilarityComputeRequest(
        ...     operation="compare",
        ...     vector_a=[0.5, 0.5, 0.5],
        ...     vector_b=[0.6, 0.4, 0.5],
        ...     metric="cosine",
        ...     threshold=0.1,
        ... )

    Raises:
        ValueError: If vectors have mismatched dimensions.
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
        """Ensure vectors have matching dimensions.

        Returns:
            Self: The validated instance.

        Raises:
            ValueError: If vector dimensions do not match.
        """
        if len(self.vector_a) != len(self.vector_b):
            msg = f"Dimension mismatch: {len(self.vector_a)} vs {len(self.vector_b)}"
            raise ValueError(msg)
        return self
