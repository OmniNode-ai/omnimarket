# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Similarity Compute Response model for vector similarity operations.

This module defines the response envelope returned by the similarity_compute node
after performing vector similarity calculations.

Example:
    >>> from omnimemory.nodes.node_similarity_compute.models import (
    ...     ModelSimilarityComputeResponse,
    ... )
    >>> # Successful response
    >>> response = ModelSimilarityComputeResponse(
    ...     status="success",
    ...     distance=0.25,
    ...     similarity=0.75,
    ...     is_match=True,
    ...     dimensions=3,
    ... )
    >>> # Error response
    >>> error_response = ModelSimilarityComputeResponse(
    ...     status="error",
    ...     error_message="Vector dimension mismatch",
    ... )

.. versionadded:: 0.1.0
    Initial implementation for OMN-1388.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelSimilarityComputeResponse"]


class ModelSimilarityComputeResponse(BaseModel):
    """Response envelope for similarity compute operations.

    This model encapsulates the results of vector similarity calculations.
    The status field indicates success or failure, and other fields provide
    operation-specific results.

    For successful operations:
        - distance: Always populated with the calculated distance
        - similarity: Populated for cosine metric (1.0 - distance)
        - is_match: Populated when a threshold was provided in the request
        - dimensions: The number of dimensions in the compared vectors

    For failed operations:
        - status: Set to "error"
        - error_message: Contains the error description

    Attributes:
        status: Operation status ("success" or "error").
        distance: Distance between vectors (cosine_distance or euclidean).
        similarity: Similarity score for cosine metric (1.0 = identical).
        is_match: Whether vectors match within the threshold.
        dimensions: Number of dimensions in the compared vectors.
        notes: Optional diagnostic or informational notes.
        error_message: Error description when status is "error".

    Example:
        >>> # Cosine distance result
        >>> cosine_result = ModelSimilarityComputeResponse(
        ...     status="success",
        ...     distance=0.134,
        ...     dimensions=512,
        ... )
        >>>
        >>> # Full comparison result
        >>> compare_result = ModelSimilarityComputeResponse(
        ...     status="success",
        ...     distance=0.05,
        ...     similarity=0.95,
        ...     is_match=True,
        ...     dimensions=1024,
        ...     notes="Vectors are highly similar",
        ... )
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "error"] = Field(
        ...,
        description="Operation status",
    )

    distance: float | None = Field(
        default=None,
        description="Distance between vectors",
    )

    similarity: float | None = Field(
        default=None,
        description="Similarity score (for cosine metric)",
    )

    is_match: bool | None = Field(
        default=None,
        description="Whether vectors match within threshold",
    )

    dimensions: int | None = Field(
        default=None,
        ge=1,
        description="Number of dimensions in compared vectors",
    )

    notes: str | None = Field(
        default=None,
        description="Optional diagnostic notes",
    )

    error_message: str | None = Field(
        default=None,
        description="Error message if status is 'error'",
    )
