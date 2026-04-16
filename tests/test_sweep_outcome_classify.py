# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 8: Tests for node_sweep_outcome_classify [OMN-8963].

Tests verify 6-branch classification table. Handler is pure — no mocks needed.
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
