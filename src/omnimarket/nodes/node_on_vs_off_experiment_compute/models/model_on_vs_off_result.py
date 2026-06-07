# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result models for node_on_vs_off_experiment_compute (OMN-12661).

EnumProofClass was promoted to omnimarket.enums.enum_proof_class in OMN-12794
(P2-1) because a second node (node_generation_consumer) also uses it.
It is re-exported here so existing callers are unaffected.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Canonical location — promoted in OMN-12794 (P2-1).
from omnimarket.enums.enum_proof_class import EnumProofClass


class ModelOnVsOffCostRow(BaseModel):
    """Per-task cost breakdown for both ON and OFF paths."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    # ON path (with OmniNode context pack)
    on_prompt_tokens: int
    on_completion_tokens: int
    on_total_tokens: int
    on_cost_usd: float
    # OFF path (baseline, no context pack)
    off_prompt_tokens: int
    off_completion_tokens: int
    off_total_tokens: int
    off_cost_usd: float
    # Delta (positive = ON costs more; negative = ON is cheaper)
    cost_delta_usd: float = Field(
        description="on_cost_usd - off_cost_usd. Negative means ON is cheaper."
    )
    token_delta: int = Field(
        description="on_total_tokens - off_total_tokens. Negative means ON uses fewer tokens."
    )


class ModelOnVsOffSummaryReport(BaseModel):
    """Aggregated summary across all tasks in the experiment run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    model_id: str
    task_count: int
    total_on_cost_usd: float
    total_off_cost_usd: float
    total_cost_delta_usd: float = Field(
        description="total_on - total_off. Negative = ON saves money overall."
    )
    total_on_tokens: int
    total_off_tokens: int
    total_token_delta: int
    cost_delta_pct: float = Field(
        description=(
            "Percentage change: (on - off) / off * 100. "
            "Negative = ON is cheaper. Zero when off_cost is zero."
        )
    )
    proof_class: EnumProofClass
    generated_at: str = Field(description="ISO8601 UTC timestamp of report generation")


class ModelOnVsOffResult(BaseModel):
    """Terminal output of the ON-vs-OFF experiment compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(description="'ok' or 'failed'")
    rows: tuple[ModelOnVsOffCostRow, ...] = Field(default_factory=tuple)
    summary: ModelOnVsOffSummaryReport | None = None
    failure_class: str | None = None
    errors: tuple[str, ...] = Field(default_factory=tuple)


__all__ = [
    "EnumProofClass",
    "ModelOnVsOffCostRow",
    "ModelOnVsOffResult",
    "ModelOnVsOffSummaryReport",
]
