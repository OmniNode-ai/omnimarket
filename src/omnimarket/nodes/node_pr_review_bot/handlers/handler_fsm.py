"""HandlerPrReviewBot — pure FSM state machine for the PR review bot lifecycle.

Phase sequence:
    INIT -> FETCH_DIFF -> REVIEW -> POST_THREADS -> WATCH -> JUDGE_VERIFY -> REPORT -> DONE
    Any non-terminal phase -> FAILED after MAX_CONSECUTIVE_FAILURES failures.

This handler is pure COMPUTE: no external I/O. Sub-handlers (diff fetcher, thread
poster, watcher, judge verifier, report poster) are injected via protocol methods
and called from run_full_pipeline() in the effects layer.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_review_bot.models.models import (
    DiffHunk,
    EnumFsmPhase,
    EnumPrVerdict,
    EnumThreadStatus,
    ReviewFinding,
    ReviewRequest,
    ReviewVerdict,
    ThreadState,
)

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 3

# ---------------------------------------------------------------------------
# Phase ordering helpers
# ---------------------------------------------------------------------------

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
    """Return the next FSM phase in the pr-review-bot progression."""
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
# FSM state
# ---------------------------------------------------------------------------


class ModelPrReviewBotState(BaseModel):
    """Immutable FSM state for the PR review bot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Matches ReviewRequest.correlation_id."
    )
    pr_number: int = Field(..., ge=1)
    repo: str = Field(...)
    current_phase: EnumFsmPhase = Field(default=EnumFsmPhase.INIT)
    consecutive_failures: int = Field(default=0, ge=0)
    dry_run: bool = Field(default=False)
    # Accumulated during FETCH_DIFF phase
    diff_hunks: tuple[DiffHunk, ...] = Field(default_factory=tuple)
    # Accumulated during REVIEW phase
    findings: tuple[ReviewFinding, ...] = Field(default_factory=tuple)
    # Accumulated during POST_THREADS / WATCH / JUDGE_VERIFY phases
    thread_states: tuple[ThreadState, ...] = Field(default_factory=tuple)
    error_message: str | None = Field(default=None)
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


# ---------------------------------------------------------------------------
# Phase transition event
# ---------------------------------------------------------------------------


class ModelPhaseTransitionEvent(BaseModel):
    """Published on every FSM phase transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    pr_number: int = Field(...)
    repo: str = Field(...)
    from_phase: EnumFsmPhase = Field(...)
    to_phase: EnumFsmPhase = Field(...)
    success: bool = Field(...)
    timestamp: datetime = Field(...)
    error_message: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# Sub-handler protocols (injected by the effects layer)
# ---------------------------------------------------------------------------


class ProtocolDiffFetcher(ABC):
    """Fetches the PR diff and full file context."""

    @abstractmethod
    def fetch(self, pr_number: int, repo: str) -> list[DiffHunk]:
        """Return diff hunks for the PR. Raises on unrecoverable failure."""
        ...


class ProtocolReviewer(ABC):
    """Fans out review to configured reviewer models."""

    @abstractmethod
    def review(
        self,
        correlation_id: UUID,
        diff_hunks: tuple[DiffHunk, ...],
        reviewer_models: list[str],
    ) -> list[ReviewFinding]:
        """Return all findings from all reviewer models."""
        ...


class ProtocolThreadPoster(ABC):
    """Posts MAJOR/CRITICAL findings as GitHub PR review threads."""

    @abstractmethod
    def post(
        self,
        pr_number: int,
        repo: str,
        findings: tuple[ReviewFinding, ...],
        dry_run: bool,
    ) -> list[ThreadState]:
        """Post threads and return initial ThreadState for each posted finding."""
        ...


class ProtocolThreadWatcher(ABC):
    """Polls GitHub for thread resolution events."""

    @abstractmethod
    def watch(
        self,
        pr_number: int,
        repo: str,
        thread_states: tuple[ThreadState, ...],
    ) -> list[ThreadState]:
        """Return updated ThreadState list after polling for resolutions."""
        ...


class ProtocolJudgeVerifier(ABC):
    """Sends resolved threads to the judge model for verification."""

    @abstractmethod
    def verify(
        self,
        correlation_id: UUID,
        findings: tuple[ReviewFinding, ...],
        thread_states: tuple[ThreadState, ...],
        judge_model: str,
    ) -> list[ThreadState]:
        """Return updated ThreadState list after judge verification."""
        ...


class ProtocolReportPoster(ABC):
    """Posts the final summary PR comment."""

    @abstractmethod
    def post_summary(
        self,
        pr_number: int,
        repo: str,
        verdict: ReviewVerdict,
        dry_run: bool,
    ) -> None:
        """Post the final summary comment. Raises on unrecoverable failure."""
        ...


# ---------------------------------------------------------------------------
# FSM handler — pure logic
# ---------------------------------------------------------------------------


class HandlerPrReviewBot:
    """Pure FSM state machine for the PR review bot.

    Callers wire sub-handlers and call run_full_pipeline(). No external I/O here.
    """

    def start(self, request: ReviewRequest) -> ModelPrReviewBotState:
        """Initialise FSM state from a ReviewRequest."""
        return ModelPrReviewBotState(
            correlation_id=request.correlation_id,
            pr_number=request.pr_number,
            repo=request.repo,
            dry_run=request.dry_run,
        )

    def advance(
        self,
        state: ModelPrReviewBotState,
        phase_success: bool,
        error_message: str | None = None,
        diff_hunks: list[DiffHunk] | None = None,
        findings: list[ReviewFinding] | None = None,
        thread_states: list[ThreadState] | None = None,
    ) -> tuple[ModelPrReviewBotState, ModelPhaseTransitionEvent]:
        """Advance the FSM by one phase.

        On success: moves to next_phase(), resets consecutive_failures.
        On failure: increments consecutive_failures; trips circuit breaker at
        MAX_CONSECUTIVE_FAILURES and transitions to FAILED.
        """
        from_phase = state.current_phase
        now = datetime.now(tz=UTC)

        if from_phase in TERMINAL_PHASES:
            msg = f"Cannot advance from terminal phase: {from_phase}"
            raise ValueError(msg)

        if not phase_success:
            new_failures = state.consecutive_failures + 1
            err: str | None
            if new_failures >= MAX_CONSECUTIVE_FAILURES:
                to_phase = EnumFsmPhase.FAILED
                err = (
                    error_message
                    or f"Circuit breaker: {new_failures} consecutive failures"
                )
            else:
                to_phase = from_phase  # retry same phase
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
        self,
        state: ModelPrReviewBotState,
        judge_model_used: str = "",
    ) -> ReviewVerdict:
        """Derive the final ReviewVerdict from completed FSM state.

        If the FSM ended in FAILED, returns BLOCKING_ISSUE to fail closed —
        a run that aborted before producing findings must not be reported as CLEAN.
        """
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
                judge_model_used=judge_model_used,
                duration_ms=int(
                    (datetime.now(tz=UTC) - state.started_at).total_seconds() * 1000
                ),
                completed_at=datetime.now(tz=UTC),
                summary=f"FSM terminated in FAILED: {state.error_message or 'unknown error'}",
            )

        threads = state.thread_states
        threads_posted = sum(1 for t in threads if t.status != EnumThreadStatus.PENDING)
        threads_pass = sum(
            1 for t in threads if t.status == EnumThreadStatus.VERIFIED_PASS
        )
        threads_fail = sum(
            1 for t in threads if t.status == EnumThreadStatus.VERIFIED_FAIL
        )
        threads_pending = sum(
            1 for t in threads if t.status == EnumThreadStatus.PENDING
        )

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
            judge_model_used=judge_model_used,
            duration_ms=int(
                (datetime.now(tz=UTC) - state.started_at).total_seconds() * 1000
            ),
            completed_at=datetime.now(tz=UTC),
        )

    def serialize_event(self, event: ModelPhaseTransitionEvent) -> bytes:
        """Serialize a phase transition event to bytes for Kafka publish."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def run_full_pipeline(
        self,
        request: ReviewRequest,
        diff_fetcher: ProtocolDiffFetcher,
        reviewer: ProtocolReviewer,
        thread_poster: ProtocolThreadPoster,
        thread_watcher: ProtocolThreadWatcher,
        judge_verifier: ProtocolJudgeVerifier,
        report_poster: ProtocolReportPoster,
    ) -> tuple[ModelPrReviewBotState, list[ModelPhaseTransitionEvent], ReviewVerdict]:
        """Execute the full FSM pipeline with injected sub-handlers.

        Returns (final_state, all_events, verdict). Callers publish events
        to Kafka. On FAILED terminal, verdict reflects the failure.
        """
        state = self.start(request)
        events: list[ModelPhaseTransitionEvent] = []

        while state.current_phase not in TERMINAL_PHASES:
            phase = state.current_phase
            try:
                if phase == EnumFsmPhase.INIT:
                    state, event = self.advance(state, phase_success=True)

                elif phase == EnumFsmPhase.FETCH_DIFF:
                    hunks = diff_fetcher.fetch(state.pr_number, state.repo)
                    state, event = self.advance(
                        state, phase_success=True, diff_hunks=hunks
                    )

                elif phase == EnumFsmPhase.REVIEW:
                    raw_findings = reviewer.review(
                        state.correlation_id,
                        state.diff_hunks,
                        request.reviewer_models,
                    )
                    state, event = self.advance(
                        state, phase_success=True, findings=raw_findings
                    )

                elif phase == EnumFsmPhase.POST_THREADS:
                    initial_threads = thread_poster.post(
                        state.pr_number,
                        state.repo,
                        state.findings,
                        state.dry_run,
                    )
                    state, event = self.advance(
                        state, phase_success=True, thread_states=initial_threads
                    )

                elif phase == EnumFsmPhase.WATCH:
                    updated_threads = thread_watcher.watch(
                        state.pr_number,
                        state.repo,
                        state.thread_states,
                    )
                    state, event = self.advance(
                        state, phase_success=True, thread_states=updated_threads
                    )

                elif phase == EnumFsmPhase.JUDGE_VERIFY:
                    verified_threads = judge_verifier.verify(
                        state.correlation_id,
                        state.findings,
                        state.thread_states,
                        request.judge_model,
                    )
                    state, event = self.advance(
                        state, phase_success=True, thread_states=verified_threads
                    )

                elif phase == EnumFsmPhase.REPORT:
                    verdict_obj = self.make_verdict(
                        state, judge_model_used=request.judge_model
                    )
                    report_poster.post_summary(
                        state.pr_number,
                        state.repo,
                        verdict_obj,
                        state.dry_run,
                    )
                    state, event = self.advance(state, phase_success=True)

                else:
                    msg = f"Unhandled phase: {phase}"
                    raise RuntimeError(msg)

            except Exception as exc:
                logger.exception("Phase %s failed: %s", phase, exc)
                state, event = self.advance(
                    state,
                    phase_success=False,
                    error_message=str(exc),
                )

            events.append(event)

            # If the FSM reached a terminal phase, stop the loop.
            # Do NOT break on non-terminal failure — advance() handles retries
            # by keeping current_phase unchanged; the circuit breaker in advance()
            # trips to FAILED after MAX_CONSECUTIVE_FAILURES, which is terminal.

        verdict = self.make_verdict(state, judge_model_used=request.judge_model)
        return state, events, verdict


__all__: list[str] = [
    "MAX_CONSECUTIVE_FAILURES",
    "TERMINAL_PHASES",
    "HandlerPrReviewBot",
    "ModelPhaseTransitionEvent",
    "ModelPrReviewBotState",
    "ProtocolDiffFetcher",
    "ProtocolJudgeVerifier",
    "ProtocolReportPoster",
    "ProtocolReviewer",
    "ProtocolThreadPoster",
    "ProtocolThreadWatcher",
    "next_phase",
]
