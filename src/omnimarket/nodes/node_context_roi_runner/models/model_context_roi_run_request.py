# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input command model for node_context_roi_runner.

One command drives the entire N-arm x K-trial experiment matrix.
The runner publishes one generation command per (task x factor_subset x trial)
over the bus; it never calls the generation consumer in-process.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelContextRoiTask(BaseModel):
    """A single generation task in the fixed task manifest.

    task_id must be stable across runs so rows can be correlated across
    separate experiment invocations.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(description="Stable task identifier — used as the row key")
    task_description: str = Field(
        description="Natural language description passed to the generation consumer"
    )
    required_factors: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Factor labels that must be present for each ON arm. "
            "A missing required factor records failure_stage=pack_build."
        ),
    )
    optional_factors: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Factor labels that are injected when available but do not fail "
            "the row if absent."
        ),
    )


class ModelContextRoiArmSpec(BaseModel):
    """A single arm of the N-arm factor matrix.

    label identifies the arm in result rows, e.g. 'off', 'golden_only',
    'golden_exemplar', 'structured_context', etc.
    factor_subset lists the EnumContextFactor values (as strings) to inject.
    An empty factor_subset is the 'off' baseline.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(description="Arm label — unique within a run")
    factor_subset: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Ordered EnumContextFactor value strings to inject for this arm. "
            "Empty tuple = off arm (no context injected)."
        ),
    )


class ModelContextRoiRunRequest(BaseModel):
    """Top-level command for the context-ROI runner EFFECT.

    Drives the full experiment: for each (task x arm x trial) the runner
    builds a context pack (REUSE: node_context_pack_builder_compute),
    publishes a generation command over the bus, consumes the terminal event,
    and emits one ModelAttemptReductionRow.

    Statistical validity:
    - trials_per_cell ≥ 3 recommended; state the chosen value.
    - arm_order_seed controls randomisation of arm order within each task.
    - max_attempts must be raised in the experiment overlay to give the
      attempt signal dynamic range; at default=2 the headline metric is
      first_pass_success rate, not mean-attempts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(description="Stable run identifier for this experiment batch")
    tasks: tuple[ModelContextRoiTask, ...] = Field(
        description="Fixed task manifest — one entry per generation task"
    )
    arms: tuple[ModelContextRoiArmSpec, ...] = Field(
        description="N-arm factor matrix — includes the 'off' baseline arm"
    )
    trials_per_cell: int = Field(
        default=3,
        ge=1,
        description=(
            "Number of trials per (task x arm) cell. "
            "K ≥ 3 recommended for variance estimation."
        ),
    )
    max_attempts: int = Field(
        default=2,
        ge=1,
        description=(
            "Maximum generation attempts forwarded to the generation consumer. "
            "Raise via overlay for attempt-signal dynamic range."
        ),
    )
    arm_order_seed: int = Field(
        default=42,
        description="Seed for arm-order randomisation within each task.",
    )
    generation_timeout_seconds: float = Field(
        default=120.0,
        gt=0.0,
        description="Per-trial timeout waiting for the terminal generation event.",
    )
    contract_hash: str = Field(
        default="",
        description=(
            "SHA-256 hex of the runner contract for provenance; "
            "passed through to context-pack assembly."
        ),
    )
    artifact_content_map: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional pre-resolved artifact content keyed by EnumContextFactor "
            "value string (e.g. 'golden_chain': '<chain text>'). "
            "When a factor label is absent from this map the handler falls back "
            "to a stub placeholder. In production this map is populated by the "
            "content-resolver effect before dispatch."
        ),
    )


__all__ = [
    "ModelContextRoiArmSpec",
    "ModelContextRoiRunRequest",
    "ModelContextRoiTask",
]
