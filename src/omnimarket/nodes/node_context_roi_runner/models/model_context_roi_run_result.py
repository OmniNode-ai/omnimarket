# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for node_context_roi_runner.

Emitted on onex.evt.omnimarket.context-roi-run-completed.v1.
Contains all captured ModelAttemptReductionRow instances so the downstream
COMPUTE scorer (node_context_roi_compute) can operate purely on this result
— no live I/O required.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_proof_class import EnumProofClass
from omnimarket.nodes.node_context_roi_runner.models.model_attempt_reduction import (
    ModelAttemptReductionRow,
)


class ModelContextRoiRunResult(BaseModel):
    """Terminal output for one full experiment run.

    rows holds one entry per (task x arm x trial).  Freeze these rows as
    fixtures so the scorer node is REPLAY_PROVEN (mirrors OMN-12661).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(description="Echoed from the request run_id")
    rows: tuple[ModelAttemptReductionRow, ...] = Field(
        description="Per-(task x arm x trial) attempt-reduction rows"
    )
    proof_class: EnumProofClass = Field(
        default=EnumProofClass.RUNTIME_OBSERVED_ONLY,
        description=(
            "REPLAY_PROVEN when rows were re-scored from frozen fixtures; "
            "RUNTIME_OBSERVED_ONLY when captured from a live run."
        ),
    )
    total_trials: int = Field(
        default=0,
        ge=0,
        description="Total number of (task x arm x trial) cells attempted",
    )
    failed_trials: int = Field(
        default=0,
        ge=0,
        description="Number of trials that ended in a non-none failure_stage",
    )
    warnings: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Non-fatal warnings from the run (e.g. optional factor absent)",
    )


__all__ = ["ModelContextRoiRunResult"]
