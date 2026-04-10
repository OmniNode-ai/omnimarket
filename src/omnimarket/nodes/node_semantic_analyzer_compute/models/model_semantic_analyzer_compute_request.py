# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Request model for semantic analyzer compute node.

.. versionadded:: 0.1.0
    Initial implementation for OMN-1390.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelSemanticAnalyzerComputeRequest(BaseModel):
    """Request model for semantic analyzer compute operations.

    Supports three operations:
        - analyze: Full semantic analysis
        - embed: Generate embedding vector
        - extract_entities: Extract named entities

    Example::

        # Embedding request
        request = ModelSemanticAnalyzerComputeRequest(
            operation="embed",
            content="Hello, world!",
        )

        # Entity extraction
        request = ModelSemanticAnalyzerComputeRequest(
            operation="extract_entities",
            content="John works at Google in NYC.",
        )

        # Full analysis
        request = ModelSemanticAnalyzerComputeRequest(
            operation="analyze",
            content="Complex text to analyze.",
            analysis_type="full",
        )
    """

    model_config = ConfigDict(extra="forbid")

    operation: Literal["analyze", "embed", "extract_entities"] = Field(
        description="The operation to perform",
    )

    content: str = Field(
        min_length=1,
        description="The text content to process",
    )

    # Optional parameters for analyze operation
    analysis_type: Literal["full", "embedding_only", "entities_only"] = Field(
        default="full",
        description="Type of analysis to perform (for analyze operation)",
    )

    # Optional model override
    model: str | None = Field(
        default=None,
        description="Optional model override for embedding generation",
    )

    # Correlation ID for tracing
    correlation_id: UUID | None = Field(
        default=None,
        description="Optional correlation ID for request tracing",
    )

    @field_validator("content")
    @classmethod
    def validate_content_not_whitespace(cls, v: str) -> str:
        """Ensure content is not just whitespace."""
        if not v.strip():
            raise ValueError("Content cannot be empty or whitespace-only")
        return v
