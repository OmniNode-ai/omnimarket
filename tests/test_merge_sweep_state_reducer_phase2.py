# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Phase 2 reducer tests for node_merge_sweep_state_reducer [OMN-8997].

Covers all six Phase 2 event paths:
  - ModelThreadRepliedEvent: success / failure
  - ModelConflictResolvedEvent: success / failure / is_noop
  - CiFixResult: success / failure / is_noop

is_noop MUST NOT increment consecutive_failures.
Success variants MUST reset consecutive_failures to 0.
Failure variants MUST increment consecutive_failures and append to last_failure_categories.
"""

from __future__ import annotations

from uuid import UUID

from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_result import CiFixResult
from omnimarket.nodes.node_merge_sweep_state_reducer.handlers.handler_sweep_state import (
    HandlerMergeSweepStateReducer,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.models.model_merge_sweep_state import (
    ModelMergeSweepState,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.models.model_phase2_events import (
    ModelConflictResolvedEvent,
)
from omnimarket.nodes.node_thread_reply_effect.models.model_thread_replied_event import (
    ModelThreadRepliedEvent,
)

_RUN_ID = UUID("00000000-0000-4000-a000-000000000001")
_CORR_ID = UUID("00000000-0000-4000-a000-000000000002")
_REPO = "OmniNode-ai/omni_home"
_PR = 42


def _base_state() -> ModelMergeSweepState:
    return ModelMergeSweepState(run_id=_RUN_ID, total_prs=1)


def _thread_event(reply_posted: bool) -> ModelThreadRepliedEvent:
    return ModelThreadRepliedEvent(
        correlation_id=_CORR_ID,
        pr_number=_PR,
        repo=_REPO,
        comment_id="gh-comment-1" if reply_posted else None,
        reply_posted=reply_posted,
        is_draft=False,
    )


def _conflict_event(
    resolution_committed: bool, is_noop: bool = False
) -> ModelConflictResolvedEvent:
    return ModelConflictResolvedEvent(
        correlation_id=_CORR_ID,
        pr_number=_PR,
        repo=_REPO,
        resolution_committed=resolution_committed,
        is_noop=is_noop,
    )


def _ci_fix_event(patch_applied: bool, is_noop: bool = False) -> CiFixResult:
    return CiFixResult(
        pr_number=_PR,
        repo=_REPO,
        run_id_github="runs/123",
        failing_job_name="test",
        correlation_id=_CORR_ID,
        patch_applied=patch_applied,
        local_tests_passed=patch_applied,
        is_noop=is_noop,
    )


# ---------------------------------------------------------------------------
# ModelThreadRepliedEvent paths
# ---------------------------------------------------------------------------


def test_thread_reply_success_resets_failures() -> None:
    """reply_posted=True: thread_replies_posted +1, consecutive_failures=0."""
    handler = HandlerMergeSweepStateReducer()
    state = _base_state()

    new_state, intents = handler.delta(state, _thread_event(reply_posted=True))

    assert new_state.thread_replies_posted == 1
    assert new_state.thread_reply_failures == 0
    key = f"{_REPO}#{_PR}"
    assert key in new_state.pr_phase2_by_key
    record = new_state.pr_phase2_by_key[key]
    assert record.consecutive_failures == 0
    assert intents  # persist intent emitted


def test_thread_reply_failure_increments_failures() -> None:
    """reply_posted=False: thread_reply_failures +1, consecutive_failures +1, category appended."""
    handler = HandlerMergeSweepStateReducer()
    state = _base_state()

    new_state, intents = handler.delta(state, _thread_event(reply_posted=False))

    assert new_state.thread_reply_failures == 1
    assert new_state.thread_replies_posted == 0
    key = f"{_REPO}#{_PR}"
    record = new_state.pr_phase2_by_key[key]
    assert record.consecutive_failures == 1
    assert "thread_reply_failed" in record.last_failure_categories
    assert intents


def test_thread_reply_failure_then_success_resets() -> None:
    """Failure followed by success resets consecutive_failures to 0."""
    handler = HandlerMergeSweepStateReducer()
    state = _base_state()

    state, _ = handler.delta(state, _thread_event(reply_posted=False))
    state, _ = handler.delta(state, _thread_event(reply_posted=True))

    key = f"{_REPO}#{_PR}"
    assert state.pr_phase2_by_key[key].consecutive_failures == 0
    assert state.thread_replies_posted == 1
    assert state.thread_reply_failures == 1  # historical count unchanged


# ---------------------------------------------------------------------------
# ModelConflictResolvedEvent paths
# ---------------------------------------------------------------------------


def test_conflict_resolved_success_resets_failures() -> None:
    """resolution_committed=True: conflicts_resolved +1, consecutive_failures=0."""
    handler = HandlerMergeSweepStateReducer()
    state = _base_state()

    new_state, intents = handler.delta(
        state, _conflict_event(resolution_committed=True)
    )

    assert new_state.conflicts_resolved == 1
    assert new_state.conflict_hunk_failures == 0
    key = f"{_REPO}#{_PR}"
    assert new_state.pr_phase2_by_key[key].consecutive_failures == 0
    assert intents


def test_conflict_resolved_failure_increments_failures() -> None:
    """resolution_committed=False, is_noop=False: failures +1, category appended."""
    handler = HandlerMergeSweepStateReducer()
    state = _base_state()

    new_state, intents = handler.delta(
        state, _conflict_event(resolution_committed=False, is_noop=False)
    )

    assert new_state.conflict_hunk_failures == 1
    assert new_state.conflicts_resolved == 0
    key = f"{_REPO}#{_PR}"
    record = new_state.pr_phase2_by_key[key]
    assert record.consecutive_failures == 1
    assert "conflict_hunk_failed" in record.last_failure_categories
    assert intents


def test_conflict_resolved_noop_does_not_increment_failures() -> None:
    """is_noop=True: no failure counted, no category appended, consecutive_failures unchanged."""
    handler = HandlerMergeSweepStateReducer()
    state = _base_state()

    new_state, intents = handler.delta(
        state, _conflict_event(resolution_committed=False, is_noop=True)
    )

    assert new_state.conflict_hunk_failures == 0
    assert new_state.conflicts_resolved == 0
    key = f"{_REPO}#{_PR}"
    # Record may or may not exist; if it does, consecutive_failures must be 0.
    if key in new_state.pr_phase2_by_key:
        assert new_state.pr_phase2_by_key[key].consecutive_failures == 0
    assert intents


# ---------------------------------------------------------------------------
# CiFixResult paths
# ---------------------------------------------------------------------------


def test_ci_fix_success_resets_failures() -> None:
    """patch_applied=True: ci_fixes_attempted +1, consecutive_failures=0."""
    handler = HandlerMergeSweepStateReducer()
    state = _base_state()

    new_state, intents = handler.delta(state, _ci_fix_event(patch_applied=True))

    assert new_state.ci_fixes_attempted == 1
    assert new_state.ci_fix_failures == 0
    key = f"{_REPO}#{_PR}"
    assert new_state.pr_phase2_by_key[key].consecutive_failures == 0
    assert intents


def test_ci_fix_failure_increments_failures() -> None:
    """patch_applied=False, is_noop=False: ci_fix_failures +1, category appended."""
    handler = HandlerMergeSweepStateReducer()
    state = _base_state()

    new_state, intents = handler.delta(state, _ci_fix_event(patch_applied=False))

    assert new_state.ci_fix_failures == 1
    assert new_state.ci_fixes_attempted == 0
    key = f"{_REPO}#{_PR}"
    record = new_state.pr_phase2_by_key[key]
    assert record.consecutive_failures == 1
    assert "ci_fix_failed" in record.last_failure_categories
    assert intents


def test_ci_fix_noop_does_not_increment_failures() -> None:
    """is_noop=True: no failure counted, consecutive_failures unchanged."""
    handler = HandlerMergeSweepStateReducer()
    state = _base_state()

    new_state, intents = handler.delta(
        state, _ci_fix_event(patch_applied=False, is_noop=True)
    )

    assert new_state.ci_fix_failures == 0
    key = f"{_REPO}#{_PR}"
    if key in new_state.pr_phase2_by_key:
        assert new_state.pr_phase2_by_key[key].consecutive_failures == 0
    assert intents


# ---------------------------------------------------------------------------
# Accumulation across multiple Phase 2 events
# ---------------------------------------------------------------------------


def test_multiple_failures_accumulate_consecutive_count() -> None:
    """Two consecutive failures on the same PR: consecutive_failures=2."""
    handler = HandlerMergeSweepStateReducer()
    state = _base_state()

    state, _ = handler.delta(state, _thread_event(reply_posted=False))
    state, _ = handler.delta(state, _ci_fix_event(patch_applied=False))

    key = f"{_REPO}#{_PR}"
    record = state.pr_phase2_by_key[key]
    assert record.consecutive_failures == 2
    assert "thread_reply_failed" in record.last_failure_categories
    assert "ci_fix_failed" in record.last_failure_categories


def test_phase2_events_do_not_affect_phase1_counters() -> None:
    """Phase 2 events must not touch armed_count, rebased_count, etc."""
    handler = HandlerMergeSweepStateReducer()
    state = _base_state()

    state, _ = handler.delta(state, _thread_event(reply_posted=False))
    state, _ = handler.delta(state, _conflict_event(resolution_committed=True))
    state, _ = handler.delta(state, _ci_fix_event(patch_applied=False))

    # All Phase 1 counters remain zero
    assert state.armed_count == 0
    assert state.rebased_count == 0
    assert state.merged_count == 0
    assert state.failed_count == 0
    assert state.stuck_count == 0
    assert state.ci_rerun_count == 0
