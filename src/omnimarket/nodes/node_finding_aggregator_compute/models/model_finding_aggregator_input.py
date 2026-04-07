# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for the finding aggregator compute node.

Related:
    - OMN-7795: Finding Aggregator COMPUTE node
    - OMN-7781: Unified LLM Workflow Migration epic
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_finding_aggregator_compute.models.model_finding_aggregator_config import (
    ModelFindingAggregatorConfig,
)


class ModelSourceFindings(BaseModel):
    """Findings from a single model/source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_name: str = Field(
        ...,
        min_length=1,
        description="Identifier of the model that produced these findings (e.g. 'deepseek-r1').",
    )
    findings: tuple[dict[str, object], ...] = Field(
        ...,
        description=(
            "Findings from this model. Each dict must contain at minimum: "
            "'rule_id', 'file_path', 'line_start', 'severity', 'normalized_message'. "
            "Uses dict representation to avoid coupling to ReviewFindingObserved import "
            "at the wire boundary; handler validates and coerces internally."
        ),
    )


class ModelFindingAggregatorInput(BaseModel):
    """Input to the finding aggregator compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Pipeline correlation ID.")
    sources: tuple[ModelSourceFindings, ...] = Field(
        ...,
        min_length=1,
        description="Findings grouped by source model. At least one source required.",
    )
    config: ModelFindingAggregatorConfig = Field(
        default_factory=ModelFindingAggregatorConfig,
        description="Aggregation configuration (thresholds, weights).",
    )


__all__: list[str] = ["ModelFindingAggregatorInput", "ModelSourceFindings"]
