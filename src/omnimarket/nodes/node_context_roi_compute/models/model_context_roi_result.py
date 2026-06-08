# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result models for node_context_roi_compute (OMN-12797 P2-3).

The scorer produces one result row per (task x arm) aggregating across trials,
plus a summary table ordered by arm label. The negative-control arm is always
reported separately and never ranked as the preferred arm.

EnumProofClass is defined locally (not cross-imported from another node's private
models) to avoid cross-node reach-in violations. The semantics mirror
node_on_vs_off_experiment_compute exactly: REPLAY_PROVEN for fixture mode,
RUNTIME_OBSERVED_ONLY for live mode.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_context_roi_compute.models.model_factor_arm import (
    EnumArmLabel,
)


class EnumProofClass(StrEnum):
    """Explicit classification of the evidence bundle provenance.

    REPLAY_PROVEN: all row data sourced from pre-captured fixtures;
        the scorer is fully deterministic and can be re-run offline.
    RUNTIME_OBSERVED_ONLY: row data captured from live runner events;
        results depend on live model state and cannot be replayed offline.
    """

    REPLAY_PROVEN = "replay-proven"
    RUNTIME_OBSERVED_ONLY = "runtime-observed-only"


class ModelArmAggregateRow(BaseModel):
    """Aggregated scores for one (task x arm) cell across K trials.

    budget_fail_count > 0 means some or all trials failed at pack assembly
    due to token budget exceeded. For full_guidance_negative_control this is
    a finding in its own right, scored separately from generation failures.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    arm_label: EnumArmLabel
    trial_count: int = Field(ge=1, description="Number of trials in this cell")
    # Attempt-reduction headline metrics
    first_pass_success_count: int = Field(
        ge=0, description="Trials where contract valid on attempt 1"
    )
    final_success_count: int = Field(
        ge=0, description="Trials where contract valid within max_attempts"
    )
    first_pass_rate: float = Field(
        ge=0.0,
        le=1.0,
        description="first_pass_success_count / trial_count",
    )
    final_success_rate: float = Field(
        ge=0.0,
        le=1.0,
        description="final_success_count / trial_count",
    )
    mean_attempt_count: float | None = Field(
        default=None,
        description=(
            "Mean attempts across all trials. "
            "With the standard 10-attempt budget, first_pass_rate remains "
            "the primary signal and mean attempts is secondary."
        ),
    )
    # Budget/failure breakdown
    budget_fail_count: int = Field(
        ge=0,
        description=(
            "Trials that failed at budget check (TOKEN_BUDGET_EXCEEDED). "
            "Scored separately; never conflated with generation failures."
        ),
    )
    generation_fail_count: int = Field(
        ge=0, description="Trials that failed during generation"
    )
    missing_required_factor_count: int = Field(
        ge=0,
        description="Trials that failed due to missing required factor",
    )
    # Token and cost (None when not captured in fixture mode)
    mean_prompt_tokens: float | None = None
    mean_completion_tokens: float | None = None
    mean_cost_usd: float | None = None
    # Warnings
    warnings: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Optional-factor absence warnings emitted for this cell",
    )


class ModelArmSummaryRow(BaseModel):
    """Cross-task aggregate for one arm.

    Deltas are computed against the 'off' baseline arm. Positive delta means
    the arm improved over baseline; negative means it regressed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    arm_label: EnumArmLabel
    is_negative_control: bool = Field(
        description=(
            "True = this arm is a waste/negative-control. "
            "MUST NOT be ranked as the preferred arm."
        )
    )
    task_count: int = Field(ge=1)
    # Headline metrics (vs off baseline)
    mean_first_pass_rate: float = Field(ge=0.0, le=1.0)
    mean_final_success_rate: float = Field(ge=0.0, le=1.0)
    first_pass_rate_delta_vs_off: float | None = Field(
        default=None,
        description=(
            "mean_first_pass_rate - off_arm_mean_first_pass_rate. "
            "Positive = better than baseline. None when off baseline absent."
        ),
    )
    final_success_rate_delta_vs_off: float | None = None
    # Cost/token deltas (None when not captured)
    mean_cost_usd: float | None = None
    cost_delta_vs_off_usd: float | None = None
    cost_per_success_usd: float | None = Field(
        default=None,
        description="mean_cost_usd / mean_final_success_rate (None when rate=0 or cost absent)",
    )
    # Budget failures (separate accounting for negative_control)
    total_budget_fail_count: int = Field(ge=0)
    total_missing_required_factor_count: int = Field(ge=0)
    # Total optional-factor warnings across all cells
    optional_factor_warning_count: int = Field(ge=0)


class ModelContextRoiResult(BaseModel):
    """Terminal output of the context-ROI scorer compute node.

    arm_rows: one row per (task x arm) cell
    arm_summary: one row per arm, aggregated across tasks, deltas vs 'off'
    preferred_arm: the arm with the best first_pass_rate_delta_vs_off
        among non-negative-control arms; None if off baseline is absent
        or all arms have zero delta.
    proof_class: REPLAY_PROVEN when fixture_mode=True; RUNTIME_OBSERVED_ONLY otherwise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(description="'ok' or 'failed'")
    run_id: str = Field(default="")
    manifest_id: str = Field(default="")
    arm_rows: tuple[ModelArmAggregateRow, ...] = Field(default_factory=tuple)
    arm_summary: tuple[ModelArmSummaryRow, ...] = Field(default_factory=tuple)
    preferred_arm: EnumArmLabel | None = Field(
        default=None,
        description=(
            "The non-negative-control arm with the highest first_pass_rate_delta_vs_off. "
            "full_guidance_negative_control is explicitly excluded from preferred ranking."
        ),
    )
    proof_class: EnumProofClass | None = None
    failure_class: str | None = None
    errors: tuple[str, ...] = Field(default_factory=tuple)
    warnings: tuple[str, ...] = Field(default_factory=tuple)


__all__ = [
    "EnumProofClass",
    "ModelArmAggregateRow",
    "ModelArmSummaryRow",
    "ModelContextRoiResult",
]
