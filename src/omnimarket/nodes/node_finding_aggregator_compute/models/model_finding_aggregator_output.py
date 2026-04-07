# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for the finding aggregator compute node.

Related:
    - OMN-7795: Finding Aggregator COMPUTE node
    - OMN-7781: Unified LLM Workflow Migration epic
"""

from __future__ import annotations

from enum import StrEnum, unique
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


@unique
class EnumAggregatedVerdict(StrEnum):
    """Verdict from aggregated review findings."""

    CLEAN = "clean"
    """No findings across any model."""

    RISKS_NOTED = "risks_noted"
    """Findings present but none at ERROR severity."""

    BLOCKING_ISSUE = "blocking_issue"
    """At least one merged finding at ERROR severity."""


class ModelAggregatedFinding(BaseModel):
    """A single merged finding produced by the aggregator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(..., min_length=1, description="Canonical rule identifier.")
    file_path: str = Field(..., min_length=1, description="Relative file path.")
    line_start: int = Field(..., gt=0, description="First line number (1-indexed).")
    line_end: int | None = Field(
        default=None, description="Last line number (inclusive), None for single-line."
    )
    severity: str = Field(
        ..., description="Merged severity (error, warning, info, hint)."
    )
    normalized_message: str = Field(
        ..., min_length=1, description="Normalized message for the finding."
    )
    source_models: tuple[str, ...] = Field(
        ...,
        min_length=1,
        description="Models that reported this finding (after dedup merge).",
    )
    weighted_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Weighted confidence score based on model weights and agreement.",
    )
    merged_count: int = Field(
        default=1,
        ge=1,
        description="Number of raw findings that were merged into this one.",
    )


class ModelFindingAggregatorOutput(BaseModel):
    """Output from the finding aggregator compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Pipeline correlation ID.")
    verdict: EnumAggregatedVerdict = Field(
        ..., description="Overall verdict from aggregated findings."
    )
    merged_findings: tuple[ModelAggregatedFinding, ...] = Field(
        default_factory=tuple,
        description="Deduplicated and merged findings.",
    )
    total_input_findings: int = Field(
        default=0, ge=0, description="Total raw findings received across all sources."
    )
    total_merged_findings: int = Field(
        default=0,
        ge=0,
        description="Count of findings after dedup merge.",
    )
    total_duplicates_removed: int = Field(
        default=0, ge=0, description="Number of duplicate findings removed."
    )
    source_model_count: int = Field(
        default=0,
        ge=0,
        description="Number of source models that contributed findings.",
    )


__all__: list[str] = [
    "EnumAggregatedVerdict",
    "ModelAggregatedFinding",
    "ModelFindingAggregatorOutput",
]
