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

from omnimarket.models.model_aggregated_finding import ModelAggregatedFinding


@unique
class EnumAggregatedVerdict(StrEnum):
    """Verdict from aggregated review findings."""

    CLEAN = "clean"
    """No findings across any model."""

    RISKS_NOTED = "risks_noted"
    """Findings present but none at ERROR severity."""

    BLOCKING_ISSUE = "blocking_issue"
    """At least one merged finding at ERROR severity."""


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
