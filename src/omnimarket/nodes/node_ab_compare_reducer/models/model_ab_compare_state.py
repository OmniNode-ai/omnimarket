# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelAbCompareState -- accumulation state for the AB compare reducer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ab_compare_reducer.models.model_inference_result_entry import (
    ModelInferenceResultEntry,
)


class ModelAbCompareState(BaseModel):
    """Mutable accumulation state for one AB compare correlation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(
        ..., description="Correlation ID for this AB compare run."
    )
    expected_count: int = Field(
        ..., description="How many inference results are expected.", gt=0
    )
    results: list[ModelInferenceResultEntry] = Field(
        default_factory=list,
        description="Inference results collected so far.",
    )
    completed: bool = Field(
        default=False,
        description="True once all expected results have arrived.",
    )


__all__: list[str] = ["ModelAbCompareState"]
