# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Configuration model for the finding aggregator.

Related:
    - OMN-7795: Finding Aggregator COMPUTE node
    - OMN-7781: Unified LLM Workflow Migration epic
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelFindingAggregatorConfig(BaseModel):
    """Configuration for weighted-union dedup aggregation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jaccard_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description=(
            "Jaccard similarity threshold for dedup. Findings with similarity "
            "above this threshold are considered duplicates and merged."
        ),
    )
    model_weights: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Weights per model name. Keys are model identifiers (e.g. 'deepseek-r1', "
            "'qwen3-coder'). Values are floats in [0, 1]. Models not listed default "
            "to equal weight (1.0 / N)."
        ),
    )
    severity_promotes_on_conflict: bool = Field(
        default=True,
        description=(
            "When merging duplicate findings with different severities, "
            "promote to the higher severity if True."
        ),
    )


__all__: list[str] = ["ModelFindingAggregatorConfig"]
