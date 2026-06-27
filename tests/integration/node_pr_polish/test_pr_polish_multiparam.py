# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""WS-5 Wave 3 — multi-parameter integration coverage for node_pr_polish.

COMPUTE node (Variant A): the PR-polish FSM is pure (no I/O). The deterministic
seam is ``run_full_pipeline(command, phase_results)`` — the live ``handle()``
path with repo+pr delegates to the workflow runner (gh/git), so the synthetic
phase-result injection drives the FSM directly. Each parametrized case feeds a
distinct ``phase_results`` map and asserts the TYPED ``ModelPrPolishCompletedEvent``
final_phase + counters and the emitted phase-transition events.

Negative control: a failing phase prevents the FSM reaching DONE — final_phase
equals the failing phase and an error_message is recorded. The circuit-breaker
test proves the FAILED terminal via the ``advance()`` primitive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_polish.handlers.handler_pr_polish import HandlerPrPolish
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_start_command import (
    ModelPrPolishStartCommand,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_state import (
    EnumPrPolishPhase,
)


def _command(**overrides: object) -> ModelPrPolishStartCommand:
    base: dict[str, object] = {
        "correlation_id": uuid4(),
        "requested_at": datetime.now(tz=UTC),
    }
    base.update(overrides)
    return ModelPrPolishStartCommand(**base)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("skip_conflicts", "phase_results", "expect"),
    [
        # All phases succeed -> walks INIT..LOCAL_REVIEW -> DONE.
        pytest.param(
            False,
            {},
            {
                "final": EnumPrPolishPhase.DONE,
                "has_error": False,
                "saw_resolve_conflicts": True,
            },
            id="all-success-done",
        ),
        # skip_conflicts -> RESOLVE_CONFLICTS phase is never entered.
        pytest.param(
            True,
            {},
            {
                "final": EnumPrPolishPhase.DONE,
                "has_error": False,
                "saw_resolve_conflicts": False,
            },
            id="skip-conflicts-done",
        ),
        # NEGATIVE CONTROL: FIX_CI fails -> FSM never advances past
        # RESOLVE_CONFLICTS (advance keeps from_phase on failure); no DONE.
        pytest.param(
            False,
            {EnumPrPolishPhase.FIX_CI: False},
            {
                "final": EnumPrPolishPhase.RESOLVE_CONFLICTS,
                "has_error": True,
                "saw_resolve_conflicts": True,
            },
            id="fix-ci-failure-stalls",
        ),
        # ADDRESS_COMMENTS fails -> FSM stalls at FIX_CI.
        pytest.param(
            False,
            {EnumPrPolishPhase.ADDRESS_COMMENTS: False},
            {
                "final": EnumPrPolishPhase.FIX_CI,
                "has_error": True,
                "saw_resolve_conflicts": True,
            },
            id="address-comments-failure-stalls",
        ),
        # LOCAL_REVIEW fails -> FSM stalls at ADDRESS_COMMENTS (no DONE).
        pytest.param(
            False,
            {EnumPrPolishPhase.LOCAL_REVIEW: False},
            {
                "final": EnumPrPolishPhase.ADDRESS_COMMENTS,
                "has_error": True,
                "saw_resolve_conflicts": True,
            },
            id="local-review-failure-stalls",
        ),
    ],
)
def test_pr_polish_pipeline_multiparam(
    skip_conflicts: bool,
    phase_results: dict[EnumPrPolishPhase, bool],
    expect: dict[str, object],
) -> None:
    handler = HandlerPrPolish()
    command = _command(skip_conflicts=skip_conflicts, pr_number=555)

    state, events, completed = handler.run_full_pipeline(command, phase_results)

    assert completed.final_phase == expect["final"]
    assert completed.pr_number == 555
    assert state.current_phase == expect["final"]
    assert (completed.error_message is not None) is expect["has_error"]
    saw_resolve = any(
        e.to_phase == EnumPrPolishPhase.RESOLVE_CONFLICTS
        or e.from_phase == EnumPrPolishPhase.RESOLVE_CONFLICTS
        for e in events
    )
    assert saw_resolve is expect["saw_resolve_conflicts"]
    # Every emitted event carries the run's correlation id.
    for event in events:
        assert event.correlation_id == command.correlation_id


@pytest.mark.integration
def test_pr_polish_circuit_breaker_trips_failed_terminal() -> None:
    """Three consecutive phase failures -> FAILED terminal via advance()."""
    handler = HandlerPrPolish()
    command = _command(pr_number=42)
    state = handler.start(command)

    # Advance into RESOLVE_CONFLICTS first (a success), then fail it 3x.
    state, _ = handler.advance(state, phase_success=True)
    assert state.current_phase == EnumPrPolishPhase.RESOLVE_CONFLICTS

    last_event = None
    for _ in range(state.max_consecutive_failures):
        state, last_event = handler.advance(
            state, phase_success=False, error_message="phase failed"
        )

    assert state.current_phase == EnumPrPolishPhase.FAILED
    assert state.consecutive_failures >= state.max_consecutive_failures
    assert state.error_message is not None
    assert last_event is not None
    assert last_event.to_phase == EnumPrPolishPhase.FAILED
    assert last_event.success is False

    completed = handler.make_completed_event(state, datetime.now(tz=UTC))
    assert completed.final_phase == EnumPrPolishPhase.FAILED
