# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for node_context_roi_compute (OMN-12797 P2-2/P2-3).

The scorer accepts a task manifest and a factor matrix and validates:
  - all required factors are declared in each arm
  - missing required factors fail the row (not warn)
  - missing optional factors are warned
  - full_guidance_negative_control is never the preferred arm

In fixture mode all token counts are pre-supplied; the scorer is offline
and produces REPLAY_PROVEN bundles. In runtime-observed mode token counts
come from live runner rows (gated on coordinated deploy per plan §Parallelization).
"""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_context_roi_compute.models.model_factor_arm import (
    EnumArmLabel,
)
from omnimarket.nodes.node_context_roi_compute.models.model_task_manifest import (
    EnumFailureStage,
)


class ModelArmRunRow(BaseModel):
    """One captured row from a single (task x arm x trial) run.

    These rows are produced by the runner EFFECT and fed to this scorer.
    In fixture mode they are pre-constructed constants (replay-proven).
    In runtime-observed mode they are captured from live events.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(description="Must match a task_id in the manifest")
    arm_label: EnumArmLabel = Field(description="Arm that produced this row")
    trial_index: int = Field(
        ge=0, description="0-based trial index within (task x arm)"
    )
    run_id: str = Field(
        description="Unique identifier minted by the runner for this run"
    )
    # Success signals
    first_pass_success: bool = Field(
        description="Contract valid on generation attempt 1"
    )
    final_success: bool = Field(description="Contract valid within max_attempts")
    attempt_count: int = Field(ge=1, description="Number of generation attempts made")
    failure_stage: EnumFailureStage = Field(
        default=EnumFailureStage.NONE,
        description=(
            "Stage at which this row failed. 'none' = ran to completion. "
            "'budget_fail' on full_guidance_negative_control is expected and "
            "scored separately from generation failures."
        ),
    )
    # Token and cost signals
    prompt_tokens: int | None = Field(
        default=None, ge=0, description="Prompt tokens used (None if not captured)"
    )
    completion_tokens: int | None = Field(
        default=None, ge=0, description="Completion tokens used (None if not captured)"
    )
    estimated_cost_usd: float | None = Field(
        default=None, ge=0.0, description="Estimated cost in USD (None if not captured)"
    )
    # Identity
    model_id: str | None = Field(
        default=None, description="Model identifier used for this run"
    )
    provider: str | None = Field(
        default=None, description="Provider identifier (e.g. 'local', 'anthropic')"
    )
    endpoint_ref: str | None = Field(
        default=None, description="Routing endpoint reference"
    )
    # Context provenance
    context_pack_hash: str | None = Field(
        default=None, description="Hash of the assembled context pack for this arm"
    )
    factor_subset_hash: str | None = Field(
        default=None, description="Hash of the factor subset for replay audit"
    )
    # Reproducibility fields
    run_order: int | None = Field(
        default=None, ge=0, description="Arm execution order within the task run"
    )
    factors_present: tuple[EnumContextFactor, ...] = Field(
        default_factory=tuple,
        description="Factors that were actually present in the resolved artifacts",
    )
    factors_warned_absent: tuple[EnumContextFactor, ...] = Field(
        default_factory=tuple,
        description="Optional factors that were absent (warnings were emitted)",
    )


class ModelContextRoiRequest(BaseModel):
    """Input to the context-ROI scorer compute node.

    fixture_mode=True: all rows are pre-captured (replay-proven offline scoring).
    fixture_mode=False: rows from live runner (runtime-observed; gated on deploy).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(description="Stable run identifier for this scoring pass")
    manifest_id: str = Field(
        description="Must match the manifest used to produce the rows"
    )
    rows: tuple[ModelArmRunRow, ...] = Field(
        min_length=1,
        description="All (task x arm x trial) rows to score",
    )
    fixture_mode: bool = Field(
        default=True,
        description=(
            "True = rows are pre-captured constants (replay-proven). "
            "False = rows from live runner (runtime-observed-only, gated on deploy)."
        ),
    )


__all__ = [
    "ModelArmRunRow",
    "ModelContextRoiRequest",
]
