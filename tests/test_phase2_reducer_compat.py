# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Phase 2 reducer compatibility end-to-end test [OMN-9002].

Drives fabricated Phase 2 completion events through the full classify →
reduce pipeline and asserts compatibility is proven, not assumed.

Six Phase 2 event variants covered:
  - THREAD_REPLY success / failure
  - CONFLICT_HUNK success / noop / failure
  - CI_FIX success / failure

Also asserts:
  - Phase 1 workflows (no polish tasks) pass through unchanged
  - Phase 2 events don't interfere with Phase 1 state fields
  - Pre-existing reducer invariants hold (dedup, exactly-once terminal)
  - No KeyError, ValidationError, or AttributeError for new event shapes
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import pytest
from omnibase_core.models.intents import (
    ModelPersistStateIntent,
)

from omnimarket.nodes.node_merge_sweep_state_reducer.handlers.handler_sweep_state import (
    HandlerMergeSweepStateReducer,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.models.model_merge_sweep_state import (
    ModelMergeSweepState,
)
from omnimarket.nodes.node_sweep_outcome_classify.handlers.handler_outcome_classify import (
    HandlerSweepOutcomeClassify,
)
from omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome import (
    EnumSweepOutcome,
    ModelSweepOutcomeClassified,
    ModelSweepOutcomeInput,
)

_RUN_ID = UUID("00000000-0000-4000-a000-000000000099")
_CORR_ID = UUID("00000000-0000-4000-a000-000000000098")
_REPO = "OmniNode-ai/omnimarket"

_classify_handler = HandlerSweepOutcomeClassify()
_reduce_handler = HandlerMergeSweepStateReducer()


def _input(**kwargs: object) -> ModelSweepOutcomeInput:
    base = {
        "pr_number": 101,
        "repo": _REPO,
        "correlation_id": _CORR_ID,
        "run_id": _RUN_ID,
        "total_prs": 1,
    }
    return ModelSweepOutcomeInput(**{**base, **kwargs})


def _classify(**kwargs: object) -> ModelSweepOutcomeClassified:
    req = _input(**kwargs)
    output = _classify_handler.handle(req)
    assert output.result is not None
    result = output.result
    assert isinstance(result, ModelSweepOutcomeClassified)
    return result


def _initial_state(total_prs: int = 1) -> ModelMergeSweepState:
    return ModelMergeSweepState(run_id=_RUN_ID, total_prs=total_prs)


def _bus_dicts(intents: Sequence[object]) -> list[dict[str, object]]:
    return [i for i in intents if isinstance(i, dict)]


# ---------------------------------------------------------------------------
# Phase 2 classify: THREAD_REPLY
# ---------------------------------------------------------------------------


def test_thread_reply_success_classifies_as_success() -> None:
    """THREAD_REPLY reply_posted=True → SUCCESS."""
    result = _classify(event_type="thread_replied", reply_posted=True)
    assert result.outcome == EnumSweepOutcome.SUCCESS
    assert result.error is None
    assert result.source_event_type == "thread_replied"


def test_thread_reply_failure_classifies_as_degraded() -> None:
    """THREAD_REPLY reply_posted=False → DEGRADED."""
    result = _classify(
        event_type="thread_replied", reply_posted=False, error="rate limited"
    )
    assert result.outcome == EnumSweepOutcome.DEGRADED
    assert result.error == "rate limited"


# ---------------------------------------------------------------------------
# Phase 2 classify: CONFLICT_HUNK
# ---------------------------------------------------------------------------


def test_conflict_hunk_success_classifies_as_success() -> None:
    """CONFLICT_HUNK resolution_committed=True → SUCCESS."""
    result = _classify(event_type="conflict_resolved", resolution_committed=True)
    assert result.outcome == EnumSweepOutcome.SUCCESS
    assert result.error is None


def test_conflict_hunk_noop_classifies_as_noop() -> None:
    """CONFLICT_HUNK is_noop=True → NOOP."""
    result = _classify(event_type="conflict_resolved", is_noop=True)
    assert result.outcome == EnumSweepOutcome.NOOP
    assert result.error is None


def test_conflict_hunk_failure_classifies_as_degraded() -> None:
    """CONFLICT_HUNK resolution_committed=False, is_noop=False → DEGRADED."""
    result = _classify(
        event_type="conflict_resolved",
        resolution_committed=False,
        is_noop=False,
        error="patch rejected",
    )
    assert result.outcome == EnumSweepOutcome.DEGRADED
    assert result.error == "patch rejected"


# ---------------------------------------------------------------------------
# Phase 2 classify: CI_FIX
# ---------------------------------------------------------------------------


def test_ci_fix_success_classifies_as_success() -> None:
    """CI_FIX patch_applied=True, local_tests_passed=True → SUCCESS."""
    result = _classify(
        event_type="ci_fix_attempted", patch_applied=True, local_tests_passed=True
    )
    assert result.outcome == EnumSweepOutcome.SUCCESS
    assert result.error is None


def test_ci_fix_failure_patch_not_applied_classifies_as_failed() -> None:
    """CI_FIX patch_applied=False → FAILED."""
    result = _classify(
        event_type="ci_fix_attempted",
        patch_applied=False,
        error="no diff produced",
    )
    assert result.outcome == EnumSweepOutcome.FAILED
    assert result.error == "no diff produced"


def test_ci_fix_noop_classifies_as_noop() -> None:
    """CI_FIX is_noop=True → NOOP (takes precedence over patch_applied)."""
    result = _classify(event_type="ci_fix_attempted", is_noop=True)
    assert result.outcome == EnumSweepOutcome.NOOP


def test_ci_fix_patch_applied_tests_failed_classifies_as_degraded() -> None:
    """CI_FIX patch_applied=True but local_tests_passed=False → DEGRADED."""
    result = _classify(
        event_type="ci_fix_attempted",
        patch_applied=True,
        local_tests_passed=False,
        error="2 tests failed",
    )
    assert result.outcome == EnumSweepOutcome.DEGRADED
    assert result.error == "2 tests failed"


# ---------------------------------------------------------------------------
# Full pipeline: classify → reduce for all six Phase 2 variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected_outcome"),
    [
        # THREAD_REPLY success
        (
            {"event_type": "thread_replied", "reply_posted": True},
            EnumSweepOutcome.SUCCESS,
        ),
        # THREAD_REPLY failure
        (
            {"event_type": "thread_replied", "reply_posted": False},
            EnumSweepOutcome.DEGRADED,
        ),
        # CONFLICT_HUNK success
        (
            {"event_type": "conflict_resolved", "resolution_committed": True},
            EnumSweepOutcome.SUCCESS,
        ),
        # CONFLICT_HUNK noop
        (
            {"event_type": "conflict_resolved", "is_noop": True},
            EnumSweepOutcome.NOOP,
        ),
        # CONFLICT_HUNK failure
        (
            {
                "event_type": "conflict_resolved",
                "resolution_committed": False,
                "is_noop": False,
            },
            EnumSweepOutcome.DEGRADED,
        ),
        # CI_FIX success
        (
            {
                "event_type": "ci_fix_attempted",
                "patch_applied": True,
                "local_tests_passed": True,
            },
            EnumSweepOutcome.SUCCESS,
        ),
        # CI_FIX failure
        (
            {"event_type": "ci_fix_attempted", "patch_applied": False},
            EnumSweepOutcome.FAILED,
        ),
    ],
)
def test_phase2_full_pipeline_classify_then_reduce(
    kwargs: dict,  # type: ignore[type-arg]
    expected_outcome: EnumSweepOutcome,
) -> None:
    """Each Phase 2 variant: classify → reduce → assert no errors, correct outcome recorded."""
    classified = _classify(**kwargs)
    assert classified.outcome == expected_outcome

    state = _initial_state(total_prs=1)
    new_state, intents = _reduce_handler.delta(state, classified)

    dedup_key = f"{_REPO}#{classified.pr_number}"
    assert dedup_key in new_state.pr_outcomes_by_key
    record = new_state.pr_outcomes_by_key[dedup_key]
    assert record.outcome == expected_outcome

    # Persist intent always present on first write
    assert sum(isinstance(i, ModelPersistStateIntent) for i in intents) == 1


# ---------------------------------------------------------------------------
# Phase 2 events don't interfere with Phase 1 state fields
# ---------------------------------------------------------------------------


def test_phase2_event_does_not_corrupt_phase1_counters() -> None:
    """Phase 2 SUCCESS/DEGRADED/NOOP events don't overwrite armed/rebased/ci_rerun counters."""
    state = _initial_state(total_prs=4)

    # Phase 1 events
    phase1_armed = ModelSweepOutcomeClassified(
        pr_number=1,
        repo=_REPO,
        correlation_id=_CORR_ID,
        run_id=_RUN_ID,
        total_prs=4,
        outcome=EnumSweepOutcome.ARMED,
        source_event_type="armed",
    )
    phase1_rebased = ModelSweepOutcomeClassified(
        pr_number=2,
        repo=_REPO,
        correlation_id=_CORR_ID,
        run_id=_RUN_ID,
        total_prs=4,
        outcome=EnumSweepOutcome.REBASED,
        source_event_type="rebase_completed",
    )

    state, _ = _reduce_handler.delta(state, phase1_armed)
    state, _ = _reduce_handler.delta(state, phase1_rebased)

    assert state.armed_count == 1
    assert state.rebased_count == 1

    # Now inject Phase 2 events
    p2_success = _classify(pr_number=3, event_type="thread_replied", reply_posted=True)
    p2_degraded = _classify(
        pr_number=4,
        event_type="conflict_resolved",
        resolution_committed=False,
        is_noop=False,
    )

    state, _ = _reduce_handler.delta(state, p2_success)
    state, _ = _reduce_handler.delta(state, p2_degraded)

    # Phase 1 counters untouched
    assert state.armed_count == 1
    assert state.rebased_count == 1
    assert state.failed_count == 0
    assert state.merged_count == 0

    # All four PRs recorded
    assert len(state.pr_outcomes_by_key) == 4


# ---------------------------------------------------------------------------
# Phase 1 workflows without polish tasks pass through unchanged
# ---------------------------------------------------------------------------


def test_phase1_only_workflow_unaffected() -> None:
    """A pure Phase 1 run (armed + rebased + failed) still reaches terminal correctly."""
    state = _initial_state(total_prs=3)

    e1 = _classify(pr_number=10, total_prs=3, event_type="armed", armed=True)
    e2 = _classify(
        pr_number=11, total_prs=3, event_type="rebase_completed", success=True
    )
    e3 = _classify(
        pr_number=12,
        total_prs=3,
        event_type="rebase_completed",
        success=False,
        conflict_files=["x.py"],
    )

    state, i1 = _reduce_handler.delta(state, e1)
    assert _bus_dicts(i1) == []
    state, i2 = _reduce_handler.delta(state, e2)
    assert _bus_dicts(i2) == []
    state, i3 = _reduce_handler.delta(state, e3)

    bus = _bus_dicts(i3)
    assert len(bus) == 1
    topic = bus[0]["topic"]
    assert isinstance(topic, str)
    assert "merge-sweep-completed" in topic
    assert state.terminal_emitted is True
    assert state.armed_count == 1
    assert state.rebased_count == 1
    assert state.stuck_count == 1


# ---------------------------------------------------------------------------
# Dedup invariant holds for Phase 2 events
# ---------------------------------------------------------------------------


def test_phase2_event_dedup_first_write_wins() -> None:
    """Duplicate Phase 2 event for same PR: state unchanged, no intents."""
    state = _initial_state(total_prs=1)
    event = _classify(event_type="thread_replied", reply_posted=True)

    state, intents_first = _reduce_handler.delta(state, event)
    assert sum(isinstance(i, ModelPersistStateIntent) for i in intents_first) == 1

    state, intents_dupe = _reduce_handler.delta(state, event)
    assert intents_dupe == []
    assert state.armed_count == 0  # SUCCESS doesn't touch armed


# ---------------------------------------------------------------------------
# Exactly-once terminal when Phase 2 events complete the run
# ---------------------------------------------------------------------------


def test_terminal_fires_exactly_once_with_mixed_phase1_phase2_events() -> None:
    """Terminal emits once when Phase 1 + Phase 2 events together account for all PRs."""
    total = 4
    state = _initial_state(total_prs=total)

    events = [
        _classify(pr_number=1, total_prs=total, event_type="armed", armed=True),
        _classify(
            pr_number=2,
            total_prs=total,
            event_type="thread_replied",
            reply_posted=True,
        ),
        _classify(
            pr_number=3,
            total_prs=total,
            event_type="conflict_resolved",
            is_noop=True,
        ),
        _classify(
            pr_number=4,
            total_prs=total,
            event_type="ci_fix_attempted",
            patch_applied=False,
        ),
    ]

    terminal_count = 0
    for event in events:
        state, intents = _reduce_handler.delta(state, event)
        terminal_count += len(_bus_dicts(intents))

    assert terminal_count == 1
    assert state.terminal_emitted is True
    assert state.completed_at is not None
    assert len(state.pr_outcomes_by_key) == total


# ---------------------------------------------------------------------------
# No KeyError / ValidationError / AttributeError on Phase 2 event shapes
# ---------------------------------------------------------------------------


def test_no_attribute_errors_on_phase2_event_shapes() -> None:
    """Handler processes all Phase 2 shapes without raising attribute errors."""
    phase2_inputs = [
        {"event_type": "thread_replied", "reply_posted": True},
        {"event_type": "thread_replied", "reply_posted": False, "error": "timeout"},
        {"event_type": "conflict_resolved", "resolution_committed": True},
        {"event_type": "conflict_resolved", "is_noop": True},
        {
            "event_type": "conflict_resolved",
            "resolution_committed": False,
            "is_noop": False,
        },
        {
            "event_type": "ci_fix_attempted",
            "patch_applied": True,
            "local_tests_passed": True,
        },
        {"event_type": "ci_fix_attempted", "patch_applied": False},
        {"event_type": "ci_fix_attempted", "is_noop": True},
        {
            "event_type": "ci_fix_attempted",
            "patch_applied": True,
            "local_tests_passed": False,
        },
    ]

    for i, kwargs in enumerate(phase2_inputs):
        req = ModelSweepOutcomeInput(
            pr_number=200 + i,
            repo=_REPO,
            correlation_id=_CORR_ID,
            run_id=_RUN_ID,
            total_prs=len(phase2_inputs),
            **kwargs,
        )
        try:
            output = _classify_handler.handle(req)
            assert output.result is not None
            result = output.result
            assert isinstance(result, ModelSweepOutcomeClassified)
            assert result.outcome is not None
        except (KeyError, AttributeError) as exc:
            pytest.fail(
                f"Phase 2 event shape {kwargs['event_type']!r} raised {type(exc).__name__}: {exc}"
            )
