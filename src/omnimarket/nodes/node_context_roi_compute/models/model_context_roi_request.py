# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request models for node_context_roi_compute (OMN-12796).

N-arm scorer: accepts frozen rows captured by the runner (one per
task x factor_subset x trial) plus a pricing table, then aggregates
per-subset statistics and computes deltas vs the `off` arm.

fixture_mode=True: caller-supplied rows with pre-captured token counts
    → proof_class=REPLAY_PROVEN.
fixture_mode=False: reserved for runtime-observed scoring (not implemented;
    gated on coordinated lane deploy per OMN-12796/plan §P2-5).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelContextRoiRow(BaseModel):
    """One captured run record: one task x factor_subset x trial.

    Captured by the runner EFFECT; scored offline by this COMPUTE node.
    estimated_cost_usd is optional: if None the scorer derives cost from
    prompt_tokens/completion_tokens + pricing table.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(description="Stable run identifier matching the request run_id")
    task_id: str = Field(description="Stable task identifier, e.g. 'task_001'")
    factor_subset: str = Field(
        description="Arm label, e.g. 'off', 'golden_only', 'golden_exemplar'"
    )
    trial_index: int = Field(ge=0, description="0-based trial index within this cell")

    # Attempt-reduction signals (HEADLINE metrics per plan §P2-5)
    attempt_count: int = Field(ge=1, description="Number of generation attempts taken")
    first_pass_success: bool = Field(
        description="Contract valid on attempt 1 (attempts[0].contract_passed)"
    )
    final_success: bool = Field(
        description="Contract valid within max_attempts (contract_passed)"
    )

    # Token counts (pre-captured; used to derive cost when estimated_cost_usd is None)
    prompt_tokens: int = Field(ge=0, description="Prompt token count for this run")
    completion_tokens: int = Field(
        ge=0, description="Completion token count for this run"
    )

    # Optional pre-computed cost; if present, scorer uses it directly
    estimated_cost_usd: float | None = Field(
        default=None,
        description="Pre-computed cost in USD, or None to derive from token counts",
    )


class ModelContextRoiPricing(BaseModel):
    """Per-model pricing in USD per 1k tokens."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_cost_per_1k: float = Field(
        ge=0.0, description="Cost per 1k prompt tokens in USD"
    )
    completion_cost_per_1k: float = Field(
        ge=0.0, description="Cost per 1k completion tokens in USD"
    )


class ModelContextRoiRequest(BaseModel):
    """Input to the N-arm context ROI COMPUTE scorer.

    rows: all captured run records across all factor subsets.
    off_arm_label: the label used for the baseline arm (typically 'off').
    fixture_mode=True: all rows carry pre-captured token counts;
        proof_class will be REPLAY_PROVEN.
    fixture_mode=False: reserved for runtime-observed mode (not implemented).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(description="Stable run identifier, e.g. 'omn-12796-run-001'")
    model_id: str = Field(description="Model identifier used across all arms")
    rows: tuple[ModelContextRoiRow, ...] = Field(
        min_length=1,
        description="All captured run records; must include at least one off-arm row",
    )
    pricing: ModelContextRoiPricing
    off_arm_label: str = Field(
        default="off",
        description="Label for the baseline arm against which deltas are computed",
    )
    fixture_mode: bool = Field(
        default=True,
        description=(
            "True = all token counts are caller-supplied (replay-proven). "
            "False = runtime-observed mode (not implemented; gated on deploy)."
        ),
    )


__all__ = [
    "ModelContextRoiPricing",
    "ModelContextRoiRequest",
    "ModelContextRoiRow",
]
