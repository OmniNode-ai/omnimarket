# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Feature vector and scored complexity result shared with the benchmark."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelComplexityFeatures(BaseModel):
    """The feature vector behind one classification, with its provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens_estimate: int
    distinct_sources: int
    output_kind: str
    verification: str
    requires_code_comprehension: bool
    selection_from_catalogue: bool
    execution_required: bool
    dependent_reasoning_steps: int
    measured: tuple[str, ...] = Field(
        description="Routing: features mechanically read from text. The benchmark "
        "retains its historical scorer-derived measurement labels."
    )
    declared: tuple[str, ...] = Field(
        description="Routing: trusted catalogue declarations. Benchmark: task "
        "declarations under the contract counting rule."
    )


class ModelComplexityClassification(BaseModel):
    """A derived rung, its score, and the points that produced it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung: str
    score: int
    points: dict[str, int]
    features: ModelComplexityFeatures
    contract_version: str
