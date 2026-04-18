# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_sweep_outcome_classify [OMN-8963, OMN-8996].

Tests verify Phase 1 (6-branch) + Phase 2 (thread_replied, conflict_resolved,
ci_fix_attempted) classification tables. Handler is pure — no mocks needed.
"""

from __future__ import annotations

from uuid import UUID

from omnimarket.nodes.node_sweep_outcome_classify.handlers.handler_outcome_classify import (
    HandlerSweepOutcomeClassify,
)
from omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome import (
    EnumSweepOutcome,
    ModelSweepOutcomeClassified,
    ModelSweepOutcomeInput,
)

_RUN_ID = UUID("00000000-0000-4000-a000-000000000001")
_CORR_ID = UUID("00000000-0000-4000-a000-000000000002")

_BASE = {
    "pr_number": 100,
    "repo": "OmniNode-ai/omni_home",
    "correlation_id": _CORR_ID,
    "run_id": _RUN_ID,
    "total_prs": 3,
}


def _classify(**kwargs: object) -> ModelSweepOutcomeClassified:
    req = ModelSweepOutcomeInput(**{**_BASE, **kwargs})
    handler = HandlerSweepOutcomeClassify()
    output = handler.handle(req)
    assert output.result is not None
    result = output.result
    assert isinstance(result, ModelSweepOutcomeClassified)
    return result


def test_armed_true_classified_as_armed() -> None:
    """armed=True → ARMED outcome."""
    result = _classify(event_type="armed", armed=True)
    assert result.outcome == EnumSweepOutcome.ARMED
    assert result.error is None


def test_armed_false_classified_as_failed() -> None:
    """armed=False → FAILED outcome."""
    result = _classify(event_type="armed", armed=False, error="auth error")
    assert result.outcome == EnumSweepOutcome.FAILED
    assert result.error == "auth error"


def test_rebase_success_classified_as_rebased() -> None:
    """rebase_completed + success=True → REBASED."""
    result = _classify(event_type="rebase_completed", success=True)
    assert result.outcome == EnumSweepOutcome.REBASED
    assert result.conflict_files == []


def test_rebase_conflict_classified_as_stuck() -> None:
    """rebase_completed + success=False + conflict_files → STUCK."""
    result = _classify(
        event_type="rebase_completed",
        success=False,
        conflict_files=["a.py", "b.py"],
        error="merge conflict",
    )
    assert result.outcome == EnumSweepOutcome.STUCK
    assert "a.py" in result.conflict_files


def test_rebase_failure_no_conflict_classified_as_failed() -> None:
    """rebase_completed + success=False + no conflict_files → FAILED."""
    result = _classify(
        event_type="rebase_completed",
        success=False,
        conflict_files=[],
        error="push rejected",
    )
    assert result.outcome == EnumSweepOutcome.FAILED


def test_ci_rerun_triggered_true_classified_correctly() -> None:
    """ci_rerun_triggered + rerun_triggered=True → CI_RERUN_TRIGGERED."""
    result = _classify(event_type="ci_rerun_triggered", rerun_triggered=True)
    assert result.outcome == EnumSweepOutcome.CI_RERUN_TRIGGERED
    assert result.error is None


def test_ci_rerun_triggered_false_classified_as_failed() -> None:
    """ci_rerun_triggered + rerun_triggered=False → FAILED."""
    result = _classify(
        event_type="ci_rerun_triggered",
        rerun_triggered=False,
        error="run not found",
    )
    assert result.outcome == EnumSweepOutcome.FAILED
    assert result.error == "run not found"


def test_unknown_event_type_classified_as_stuck() -> None:
    """Unknown event_type → STUCK (safe fallback)."""
    result = _classify(event_type="something_weird")
    assert result.outcome == EnumSweepOutcome.STUCK
    assert "unknown_event_type" in (result.error or "")


def test_classified_result_carries_metadata() -> None:
    """Output carries pr_number, repo, correlation_id, run_id, total_prs."""
    result = _classify(event_type="armed", armed=True)
    assert result.pr_number == 100
    assert result.repo == "OmniNode-ai/omni_home"
    assert result.correlation_id == _CORR_ID
    assert result.run_id == _RUN_ID
    assert result.total_prs == 3
    assert result.source_event_type == "armed"


def test_handler_is_pure_no_io() -> None:
    """Same input always produces same output (pure function check)."""
    req = ModelSweepOutcomeInput(
        event_type="armed",
        armed=True,
        pr_number=200,
        repo="OmniNode-ai/omni_home",
        correlation_id=_CORR_ID,
        run_id=_RUN_ID,
        total_prs=5,
    )
    handler = HandlerSweepOutcomeClassify()
    out1 = handler.handle(req)
    out2 = handler.handle(req)
    assert out1.result is not None
    assert out2.result is not None
    r1 = out1.result
    r2 = out2.result
    assert isinstance(r1, ModelSweepOutcomeClassified)
    assert isinstance(r2, ModelSweepOutcomeClassified)
    assert r1.outcome == r2.outcome
    assert r1.error == r2.error
    assert r1.pr_number == r2.pr_number


# ---------------------------------------------------------------------------
# Phase 2: thread_replied
# ---------------------------------------------------------------------------


def test_thread_replied_posted_classified_as_success() -> None:
    """thread_replied + reply_posted=True → SUCCESS."""
    result = _classify(event_type="thread_replied", reply_posted=True)
    assert result.outcome == EnumSweepOutcome.SUCCESS
    assert result.error is None


def test_thread_replied_not_posted_classified_as_degraded() -> None:
    """thread_replied + reply_posted=False → DEGRADED."""
    result = _classify(
        event_type="thread_replied", reply_posted=False, error="llm refused"
    )
    assert result.outcome == EnumSweepOutcome.DEGRADED
    assert result.error == "llm refused"


# ---------------------------------------------------------------------------
# Phase 2: conflict_resolved
# ---------------------------------------------------------------------------


def test_conflict_resolved_committed_classified_as_success() -> None:
    """conflict_resolved + resolution_committed=True → SUCCESS."""
    result = _classify(event_type="conflict_resolved", resolution_committed=True)
    assert result.outcome == EnumSweepOutcome.SUCCESS
    assert result.error is None


def test_conflict_resolved_noop_classified_as_noop() -> None:
    """conflict_resolved + is_noop=True → NOOP."""
    result = _classify(
        event_type="conflict_resolved", resolution_committed=False, is_noop=True
    )
    assert result.outcome == EnumSweepOutcome.NOOP
    assert result.error is None


def test_conflict_resolved_not_committed_not_noop_classified_as_degraded() -> None:
    """conflict_resolved + resolution_committed=False + is_noop=False → DEGRADED."""
    result = _classify(
        event_type="conflict_resolved",
        resolution_committed=False,
        is_noop=False,
        error="patch rejected",
    )
    assert result.outcome == EnumSweepOutcome.DEGRADED
    assert result.error == "patch rejected"


# ---------------------------------------------------------------------------
# Phase 2: ci_fix_attempted
# ---------------------------------------------------------------------------


def test_ci_fix_attempted_patch_applied_tests_passed_classified_as_success() -> None:
    """ci_fix_attempted + patch_applied=True + local_tests_passed=True → SUCCESS."""
    result = _classify(
        event_type="ci_fix_attempted", patch_applied=True, local_tests_passed=True
    )
    assert result.outcome == EnumSweepOutcome.SUCCESS
    assert result.error is None


def test_ci_fix_attempted_noop_classified_as_noop() -> None:
    """ci_fix_attempted + is_noop=True → NOOP (checked before patch_applied)."""
    result = _classify(event_type="ci_fix_attempted", is_noop=True)
    assert result.outcome == EnumSweepOutcome.NOOP
    assert result.error is None


def test_ci_fix_attempted_patch_not_applied_classified_as_failed() -> None:
    """ci_fix_attempted + patch_applied=False → FAILED."""
    result = _classify(
        event_type="ci_fix_attempted",
        patch_applied=False,
        local_tests_passed=False,
        error="diff parse error",
    )
    assert result.outcome == EnumSweepOutcome.FAILED
    assert result.error == "diff parse error"


def test_ci_fix_attempted_patch_applied_tests_failed_classified_as_degraded() -> None:
    """ci_fix_attempted + patch_applied=True + local_tests_passed=False → DEGRADED."""
    result = _classify(
        event_type="ci_fix_attempted",
        patch_applied=True,
        local_tests_passed=False,
        error="test_foo failed",
    )
    assert result.outcome == EnumSweepOutcome.DEGRADED
    assert result.error == "test_foo failed"
