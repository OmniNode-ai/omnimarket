# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure PR-review FSM logic (OMN-13212 / B2).

OWNER module for the PR-review state machine, re-expressed canonically from the
deleted ``node_pr_review_bot`` ``HandlerPrReviewBot`` FSM. Pure fold:
``(state, phase-outcome) -> (new_state, transition_event)``. No I/O, no bus, no
DB — the reducer folds these into the FSM state projection; the orchestrator
drives the phase work and publishes the transition events over the bus.

Phase sequence:
    INIT -> FETCH_DIFF -> REVIEW -> POST_THREADS -> WATCH -> JUDGE_VERIFY ->
    REPORT -> DONE
    Any non-terminal phase -> FAILED after ``MAX_CONSECUTIVE_FAILURES``
    consecutive failures (3-failure circuit breaker).

This logic lives in shared ``omnimarket.review`` so the REDUCER node and the
ORCHESTRATOR import one owner — no cross-node reach-in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.review.pr_review_io import (
    DiffHunk,
    EnumFsmPhase,
    EnumPrVerdict,
    EnumThreadStatus,
    ReviewFinding,
    ReviewRequest,
    ReviewVerdict,
    ThreadState,
)

MAX_CONSECUTIVE_FAILURES = 3

_PHASE_ORDER: tuple[EnumFsmPhase, ...] = (
    EnumFsmPhase.FETCH_DIFF,
    EnumFsmPhase.REVIEW,
    EnumFsmPhase.POST_THREADS,
    EnumFsmPhase.WATCH,
    EnumFsmPhase.JUDGE_VERIFY,
    EnumFsmPhase.REPORT,
)

TERMINAL_PHASES: frozenset[EnumFsmPhase] = frozenset(
    {EnumFsmPhase.DONE, EnumFsmPhase.FAILED}
)


def next_phase(current: EnumFsmPhase) -> EnumFsmPhase:
    """Return the next FSM phase in the pr-review progression.

    Raises ``ValueError`` on an attempt to advance from a terminal phase — the
    negative/reject path exercised by the reducer's golden chains.
    """
    if current == EnumFsmPhase.INIT:
        return EnumFsmPhase.FETCH_DIFF
    if current == EnumFsmPhase.REPORT:
        return EnumFsmPhase.DONE
    if current in TERMINAL_PHASES:
        msg = f"No next phase from terminal state: {current}"
        raise ValueError(msg)
    idx = _PHASE_ORDER.index(current)
    return _PHASE_ORDER[idx + 1]


# ---------------------------------------------------------------------------
# FSM state projection
# ---------------------------------------------------------------------------


class ModelPrReviewBotState(BaseModel):
    """Immutable FSM state projection for the PR review run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Matches ReviewRequest.correlation_id."
    )
    pr_number: int = Field(..., ge=1)
    repo: str = Field(...)
    current_phase: EnumFsmPhase = Field(default=EnumFsmPhase.INIT)
    consecutive_failures: int = Field(default=0, ge=0)
    max_consecutive_failures: int = Field(default=MAX_CONSECUTIVE_FAILURES, ge=1)
    dry_run: bool = Field(default=False)
    judge_model: str = Field(default="")
    diff_hunks: tuple[DiffHunk, ...] = Field(default_factory=tuple)
    findings: tuple[ReviewFinding, ...] = Field(default_factory=tuple)
    thread_states: tuple[ThreadState, ...] = Field(default_factory=tuple)
    error_message: str | None = Field(default=None)
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class ModelPhaseTransitionEvent(BaseModel):
    """Published on every FSM phase transition (pr-review-bot-phase-transition)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    pr_number: int = Field(...)
    repo: str = Field(...)
    from_phase: EnumFsmPhase = Field(...)
    to_phase: EnumFsmPhase = Field(...)
    success: bool = Field(...)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    error_message: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# Pure fold functions
# ---------------------------------------------------------------------------


def start_state(request: ReviewRequest) -> ModelPrReviewBotState:
    """Initialise the FSM state projection from a ReviewRequest."""
    return ModelPrReviewBotState(
        correlation_id=request.correlation_id,
        pr_number=request.pr_number,
        repo=request.repo,
        dry_run=request.dry_run,
        judge_model=request.judge_model,
    )


def advance(
    state: ModelPrReviewBotState,
    phase_success: bool,
    error_message: str | None = None,
    diff_hunks: tuple[DiffHunk, ...] | None = None,
    findings: tuple[ReviewFinding, ...] | None = None,
    thread_states: tuple[ThreadState, ...] | None = None,
) -> tuple[ModelPrReviewBotState, ModelPhaseTransitionEvent]:
    """Advance the FSM by one phase. Pure fold.

    On success: moves to ``next_phase``, resets the failure counter, and folds in
    any accumulated diff/findings/threads.
    On failure: increments the failure counter; trips the circuit breaker to
    ``FAILED`` at ``max_consecutive_failures``, else retries the phase in place.

    Raises ``ValueError`` when called on a terminal phase (negative/reject path).
    """
    from_phase = state.current_phase

    if from_phase in TERMINAL_PHASES:
        msg = f"Cannot advance from terminal phase: {from_phase}"
        raise ValueError(msg)

    now = datetime.now(tz=UTC)

    if not phase_success:
        new_failures = state.consecutive_failures + 1
        err: str | None
        if new_failures >= state.max_consecutive_failures:
            to_phase = EnumFsmPhase.FAILED
            err = (
                error_message or f"Circuit breaker: {new_failures} consecutive failures"
            )
        else:
            to_phase = from_phase
            err = error_message
        new_state = state.model_copy(
            update={
                "current_phase": to_phase,
                "consecutive_failures": new_failures,
                "error_message": err,
            }
        )
        event = ModelPhaseTransitionEvent(
            correlation_id=state.correlation_id,
            pr_number=state.pr_number,
            repo=state.repo,
            from_phase=from_phase,
            to_phase=to_phase,
            success=False,
            timestamp=now,
            error_message=err,
        )
        return new_state, event

    to_phase = next_phase(from_phase)
    updates: dict[str, object] = {
        "current_phase": to_phase,
        "consecutive_failures": 0,
        "error_message": None,
    }
    if diff_hunks is not None:
        updates["diff_hunks"] = tuple(diff_hunks)
    if findings is not None:
        updates["findings"] = tuple(findings)
    if thread_states is not None:
        updates["thread_states"] = tuple(thread_states)

    new_state = state.model_copy(update=updates)
    event = ModelPhaseTransitionEvent(
        correlation_id=state.correlation_id,
        pr_number=state.pr_number,
        repo=state.repo,
        from_phase=from_phase,
        to_phase=to_phase,
        success=True,
        timestamp=now,
    )
    return new_state, event


def make_verdict(
    state: ModelPrReviewBotState,
    judge_model_used: str = "",
) -> ReviewVerdict:
    """Derive the final ReviewVerdict from completed FSM state.

    If the FSM ended in FAILED, returns BLOCKING_ISSUE to fail closed — a run
    that aborted before producing findings must not be reported as CLEAN.
    """
    judge_used = judge_model_used or state.judge_model
    duration_ms = int((datetime.now(tz=UTC) - state.started_at).total_seconds() * 1000)

    if state.current_phase == EnumFsmPhase.FAILED:
        return ReviewVerdict(
            correlation_id=state.correlation_id,
            pr_number=state.pr_number,
            repo=state.repo,
            verdict=EnumPrVerdict.BLOCKING_ISSUE,
            total_findings=len(state.findings),
            threads_posted=0,
            threads_verified_pass=0,
            threads_verified_fail=0,
            threads_pending=0,
            judge_model_used=judge_used,
            duration_ms=duration_ms,
            completed_at=datetime.now(tz=UTC),
            summary=f"FSM terminated in FAILED: {state.error_message or 'unknown error'}",
        )

    threads = state.thread_states
    threads_posted = sum(1 for t in threads if t.status != EnumThreadStatus.PENDING)
    threads_pass = sum(1 for t in threads if t.status == EnumThreadStatus.VERIFIED_PASS)
    threads_fail = sum(1 for t in threads if t.status == EnumThreadStatus.VERIFIED_FAIL)
    threads_pending = sum(1 for t in threads if t.status == EnumThreadStatus.PENDING)

    if threads_fail > 0:
        verdict = EnumPrVerdict.BLOCKING_ISSUE
    elif state.findings:
        verdict = EnumPrVerdict.RISKS_NOTED
    else:
        verdict = EnumPrVerdict.CLEAN

    return ReviewVerdict(
        correlation_id=state.correlation_id,
        pr_number=state.pr_number,
        repo=state.repo,
        verdict=verdict,
        total_findings=len(state.findings),
        threads_posted=threads_posted,
        threads_verified_pass=threads_pass,
        threads_verified_fail=threads_fail,
        threads_pending=threads_pending,
        judge_model_used=judge_used,
        duration_ms=duration_ms,
        completed_at=datetime.now(tz=UTC),
    )


__all__: list[str] = [
    "MAX_CONSECUTIVE_FAILURES",
    "TERMINAL_PHASES",
    "ModelPhaseTransitionEvent",
    "ModelPrReviewBotState",
    "advance",
    "make_verdict",
    "next_phase",
    "start_state",
]
