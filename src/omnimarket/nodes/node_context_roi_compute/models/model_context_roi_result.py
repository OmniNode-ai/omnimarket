# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result models for node_context_roi_compute (OMN-12796).

N-arm scorer result: one ModelContextRoiSubsetSummary per factor_subset,
each carrying per-subset aggregates and deltas vs the off arm.

HEADLINE metrics (per plan §P2-5):
  first_pass_rate + cost_per_success_usd (NOT mean_attempts at max_attempts=2).

EnumProofClass re-exported from OMN-12661 pattern for consistency.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumProofClass(StrEnum):
    """Explicit classification of evidence bundle provenance.

    REPLAY_PROVEN: all rows sourced from pre-captured fixtures;
        scorer is fully deterministic and offline-capable.
    RUNTIME_OBSERVED_ONLY: rows captured from live inference;
        results depend on model state and cannot be replayed offline.
    """

    REPLAY_PROVEN = "replay-proven"
    RUNTIME_OBSERVED_ONLY = "runtime-observed-only"


class ModelContextRoiSubsetSummary(BaseModel):
    """Aggregated statistics for one factor_subset arm.

    All delta_vs_off fields are (this_arm - off_arm); positive means
    this arm is higher than off. For first_pass_rate a positive delta
    is an improvement; for cost_per_success a negative delta is an
    improvement (cheaper per success).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor_subset: str = Field(description="Arm label, e.g. 'off', 'golden_only'")
    row_count: int = Field(description="Number of rows scored for this subset")

    # Attempt-reduction signals
    mean_attempts: float = Field(description="Mean attempt_count across all rows")
    median_attempts: float = Field(description="Median attempt_count across all rows")
    attempt_count_variance: float = Field(
        description="Population variance of attempt_count (0 if single row)"
    )

    # HEADLINE: first-pass and final-pass rates
    first_pass_rate: float = Field(
        description="Fraction of rows where first_pass_success=True"
    )
    final_pass_rate: float = Field(
        description="Fraction of rows where final_success=True"
    )

    # Token metrics (mean per-row)
    mean_prompt_tokens: float = Field(description="Mean prompt_tokens across all rows")
    mean_completion_tokens: float = Field(
        description="Mean completion_tokens across all rows"
    )

    # Cost metrics
    total_cost_usd: float = Field(description="Sum of per-row costs in USD")
    cost_per_success_usd: float | None = Field(
        description=(
            "total_cost_usd / successful_final_trials. "
            "None when no trials have final_success=True."
        )
    )

    # Deltas vs off arm (this_arm - off_arm)
    first_pass_rate_delta_vs_off: float = Field(
        description="first_pass_rate - off_arm.first_pass_rate"
    )
    final_pass_rate_delta_vs_off: float = Field(
        description="final_pass_rate - off_arm.final_pass_rate"
    )
    mean_prompt_token_delta_vs_off: float = Field(
        description="mean_prompt_tokens - off_arm.mean_prompt_tokens"
    )
    mean_completion_token_delta_vs_off: float = Field(
        description="mean_completion_tokens - off_arm.mean_completion_tokens"
    )
    cost_per_success_delta_vs_off: float | None = Field(
        description=(
            "cost_per_success_usd - off_arm.cost_per_success_usd. "
            "None when either arm has no successes."
        )
    )


class ModelContextRoiResult(BaseModel):
    """Terminal output of the N-arm context ROI COMPUTE scorer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(description="'ok' or 'failed'")
    run_id: str = Field(description="Echoed from request")
    model_id: str = Field(description="Echoed from request")
    proof_class: EnumProofClass = Field(
        default=EnumProofClass.REPLAY_PROVEN,
        description="Evidence bundle provenance classification",
    )
    subset_summaries: tuple[ModelContextRoiSubsetSummary, ...] | None = Field(
        default=None,
        description="Per-arm aggregates; None on failure",
    )
    failure_class: str | None = None
    errors: tuple[str, ...] = Field(default_factory=tuple)
    generated_at: str = Field(description="ISO8601 UTC timestamp of report generation")


__all__ = [
    "EnumProofClass",
    "ModelContextRoiResult",
    "ModelContextRoiSubsetSummary",
]
