# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Output model for node_antipattern_match_effect. [OMN-11919]"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_antipattern_match_effect.models.model_antipattern_match import (
    ModelAntipatternMatch,
)


class ModelAntipatternMatchResponse(BaseModel):
    """Response from antipattern similarity search."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str = Field(..., min_length=1, description="Propagated from input")
    matches: tuple[ModelAntipatternMatch, ...] = Field(
        default=(),
        description="Matched antipatterns ordered by adjusted_score descending.",
    )
    query_text_used: str = Field(
        default="",
        description="The text that was embedded for the query.",
    )
    qdrant_collection: str = Field(
        default="",
        description="Qdrant collection that was queried.",
    )
    total_candidates_searched: int = Field(
        default=0,
        ge=0,
        description="Number of candidate results returned from Qdrant before filtering.",
    )
    error_message: str | None = Field(
        default=None,
        description="Non-fatal error or warning message if partial results returned.",
    )


__all__ = ["ModelAntipatternMatchResponse"]
