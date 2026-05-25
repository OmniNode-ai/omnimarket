# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Match result model for node_antipattern_match_effect. [OMN-11919]"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAntipatternMatch(BaseModel):
    """A single antipattern match from vector similarity search."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    similarity_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Cosine similarity score between query and antipattern (0.0-1.0).",
    )
    adjusted_score: float = Field(
        ...,
        ge=0.0,
        description="Score after freshness decay boost is applied.",
    )
    antipattern_name: str = Field(
        ...,
        min_length=1,
        description="Canonical antipattern name from registry.",
    )
    severity: str = Field(
        ...,
        description="Severity level: ERROR, WARNING, or INFO.",
    )
    enforcement: str = Field(
        ...,
        description="Enforcement posture: blocking, advisory, or informational.",
    )
    category: str = Field(
        ...,
        description="Antipattern category (e.g. architecture, testing, naming).",
    )
    description: str = Field(
        ...,
        description="Antipattern description from the registry entry.",
    )
    rationale: str = Field(
        default="",
        description="Why this antipattern matters.",
    )
    explanation: str = Field(
        default="",
        description="Why the query matched this antipattern (generated summary).",
    )
    source_ticket: str = Field(
        default="",
        description="Originating ticket that introduced this antipattern rule.",
    )
    registry_version: str = Field(
        default="",
        description="Registry version at index time.",
    )


__all__ = ["ModelAntipatternMatch"]
