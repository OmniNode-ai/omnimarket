# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Response model for semantic analyzer compute node.

Migrated from omnimemory to omnimarket (OMN-8297).
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from omnimemory.models.intelligence import ModelSemanticEntityList
from pydantic import BaseModel, ConfigDict, Field


class ModelSemanticAnalyzerComputeResponse(BaseModel):
    """Response model for semantic analyzer compute operations."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "error"] = Field(
        description="Operation status",
    )

    operation: Literal["analyze", "embed", "extract_entities"] = Field(
        description="The operation that was performed",
    )

    # Embedding result (for embed and analyze operations)
    embedding: list[float] | None = Field(
        default=None,
        description="Generated embedding vector",
    )

    embedding_dimension: int | None = Field(
        default=None,
        description="Dimension of the embedding vector",
    )

    # Entity extraction results (for extract_entities and analyze operations)
    entities: ModelSemanticEntityList | None = Field(
        default=None,
        description="Extracted entities",
    )

    # Topic extraction (for analyze operation)
    topics: list[str] | None = Field(
        default=None,
        description="Extracted topics",
    )

    key_concepts: list[str] | None = Field(
        default=None,
        description="Key concepts identified",
    )

    # Analysis scores (for analyze operation)
    confidence_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Overall confidence score",
    )

    complexity_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Content complexity score",
    )

    readability_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Content readability score",
    )

    # Metadata
    result_id: UUID | None = Field(
        default=None,
        description="Unique identifier for the result",
    )

    model_name: str | None = Field(
        default=None,
        description="Model used for processing",
    )

    processing_time_ms: int | None = Field(
        default=None,
        ge=0,
        description="Processing time in milliseconds",
    )

    # Error information
    error_message: str | None = Field(
        default=None,
        description="Error message if status is error",
    )
