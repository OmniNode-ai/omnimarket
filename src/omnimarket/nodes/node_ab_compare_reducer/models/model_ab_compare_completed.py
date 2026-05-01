# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelAbCompareCompleted -- terminal event payload for onex.evt.omnimarket.ab-compare-completed.v1."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ab_compare_reducer.models.model_comparison_row import (
    ModelComparisonRow,
)


class ModelAbCompareCompleted(BaseModel):
    """Payload emitted when the full AB comparison is ready."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(
        ..., description="Correlation ID for this AB compare run."
    )
    rows: list[ModelComparisonRow] = Field(
        default_factory=list,
        description="Comparison rows sorted by cost_usd ascending.",
    )
    savings_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Max cloud model cost minus minimum local model cost.",
    )
    model_count: int = Field(
        default=0, ge=0, description="Number of models in the comparison."
    )


__all__: list[str] = ["ModelAbCompareCompleted"]
