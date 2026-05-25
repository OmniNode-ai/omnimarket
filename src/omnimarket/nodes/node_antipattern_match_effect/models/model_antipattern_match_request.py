# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Input model for node_antipattern_match_effect. [OMN-11919]"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAntipatternMatchRequest(BaseModel):
    """Command payload for antipattern similarity search."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str = Field(..., min_length=1, description="Propagated to result")
    code_text: str | None = Field(
        default=None,
        description="Raw code or prose to search against the antipattern vector store.",
    )
    description: str | None = Field(
        default=None,
        description="Natural-language description of a potential antipattern.",
    )
    min_similarity: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description="Minimum cosine similarity threshold (0.0-1.0).",
    )
    max_results: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Maximum number of matches to return.",
    )
    qdrant_collection_override: str | None = Field(
        default=None,
        description="Override ANTIPATTERN_QDRANT_COLLECTION for this invocation.",
    )
    embedding_endpoint_override: str | None = Field(
        default=None,
        description="Override EMBEDDING_MODEL_URL for this invocation.",
    )
    freshness_decay_factor: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description=(
            "Weight applied to recently-discovered antipatterns. "
            "Score boost = freshness_decay_factor * recency_weight. "
            "Set to 0.0 to disable freshness ranking."
        ),
    )


__all__ = ["ModelAntipatternMatchRequest"]
