# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Response model for vector similarity compute operations.

Migrated from omnimemory (OMN-8297, Wave 1).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelSimilarityComputeResponse"]


class ModelSimilarityComputeResponse(BaseModel):
    """Response envelope for similarity compute operations.

    Attributes:
        status: Operation status ("success" or "error").
        distance: Distance between vectors.
        similarity: Similarity score for cosine metric (1.0 = identical).
        is_match: Whether vectors match within the threshold.
        dimensions: Number of dimensions in the compared vectors.
        notes: Optional diagnostic notes.
        error_message: Error description when status is "error".
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
