# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Advance command for the PR-review FSM reducer (OMN-13212 / B2).

The reducer folds one phase-advance event into the FSM state projection. The
prior state, the phase outcome, an optional error message, and any data the
just-completed phase produced (diff hunks, findings, thread states) are the only
inputs the pure transition needs.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.review.pr_review_fsm import ModelPrReviewBotState
from omnimarket.review.pr_review_io import DiffHunk, ReviewFinding, ThreadState


class ModelPrReviewAdvanceCommand(BaseModel):
    """One phase-advance event folded into the PR-review FSM state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelPrReviewBotState = Field(
        ..., description="Prior FSM state to advance from."
    )
    phase_success: bool = Field(
        ..., description="True if the just-completed phase succeeded."
    )
    error_message: str | None = Field(
        default=None, description="Error from the failing phase, if any."
    )
    diff_hunks: tuple[DiffHunk, ...] | None = Field(
        default=None, description="Diff hunks produced by FETCH_DIFF, if any."
    )
    findings: tuple[ReviewFinding, ...] | None = Field(
        default=None, description="Findings produced by REVIEW, if any."
    )
    thread_states: tuple[ThreadState, ...] | None = Field(
        default=None,
        description="Thread states produced by POST_THREADS/WATCH/JUDGE_VERIFY, if any.",
    )


__all__: list[str] = ["ModelPrReviewAdvanceCommand"]
