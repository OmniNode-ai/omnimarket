# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merged review finding shared by the finding aggregator and its consumers.

Promoted from node_finding_aggregator_compute so other nodes (the hostile
reviewer orchestrator) can carry aggregated findings without reaching into
that node's models package.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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


__all__: list[str] = ["ModelAggregatedFinding"]
