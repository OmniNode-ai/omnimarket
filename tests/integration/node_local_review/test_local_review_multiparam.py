# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_local_review [OMN-13684].

WS-5 Wave 10. Variant A (direct in-process handler call). node_local_review is a
pure FSM (INIT -> REVIEW -> FIX -> COMMIT -> CHECK_CLEAN -> DONE, looping back to
REVIEW until clean, with a 3-strike circuit breaker -> FAILED). Each case drives
the real ``advance()`` transition logic with a scripted sequence of phase
outcomes and asserts the terminal state + completed-event payload (final phase,
iteration count, issues found/fixed, error). Negative control: three consecutive
review failures must trip the circuit breaker to FAILED.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_local_review.handlers.handler_local_review import (
    HandlerLocalReview,
)
from omnimarket.nodes.node_local_review.models.model_local_review_completed_event import (
    ModelLocalReviewCompletedEvent,
)
from omnimarket.nodes.node_local_review.models.model_local_review_start_command import (
    ModelLocalReviewStartCommand,
)
from omnimarket.nodes.node_local_review.models.model_local_review_state import (
    TERMINAL_PHASES,
    EnumLocalReviewPhase,
    ModelLocalReviewState,
)


def _drive(
    handler: HandlerLocalReview,
    command: ModelLocalReviewStartCommand,
    steps: list[dict[str, Any]],
) -> tuple[ModelLocalReviewState, ModelLocalReviewCompletedEvent, int]:
    """Apply a scripted list of advance() steps and build the completed event."""
    state = handler.start(command)
    started_at = datetime.now(tz=UTC)
    applied = 0
    for step in steps:
        if state.current_phase in TERMINAL_PHASES:
            break
        state, _event = handler.advance(state, **step)
        applied += 1
    completed = handler.make_completed_event(state, started_at)
    return state, completed, applied


_OK = {"phase_success": True}
_OK_CLEAN = {"phase_success": True, "is_clean": True}
_OK_DIRTY = {"phase_success": True, "is_clean": False}


# (case_id, dry_run, steps, expected)
CASES = [
    pytest.param(
        False,
        [
            _OK,  # INIT -> REVIEW
            {
                "phase_success": True,
                "issues_found": 3,
                "issues_fixed": 3,
            },  # REVIEW->FIX
            _OK,  # FIX -> COMMIT
            _OK,  # COMMIT -> CHECK_CLEAN
            _OK_CLEAN,  # CHECK_CLEAN -> DONE
        ],
        {
            "final_phase": EnumLocalReviewPhase.DONE,
            "iteration_count": 1,
            "issues_found": 3,
            "issues_fixed": 3,
            "error": None,
        },
        id="clean-single-pass",
    ),
    pytest.param(
        False,
        [
            _OK,  # INIT -> REVIEW (iter 1)
            {
                "phase_success": True,
                "issues_found": 5,
                "issues_fixed": 2,
            },  # REVIEW->FIX
            _OK,  # FIX -> COMMIT
            _OK,  # COMMIT -> CHECK_CLEAN
            _OK_DIRTY,  # CHECK_CLEAN -> REVIEW (not clean; iter 2)
            {
                "phase_success": True,
                "issues_found": 0,
                "issues_fixed": 3,
            },  # REVIEW->FIX
            _OK,  # FIX -> COMMIT
            _OK,  # COMMIT -> CHECK_CLEAN
            _OK_CLEAN,  # CHECK_CLEAN -> DONE
        ],
        {
            "final_phase": EnumLocalReviewPhase.DONE,
            "iteration_count": 2,
            "issues_found": 5,
            "issues_fixed": 5,
            "error": None,
        },
        id="findings-then-iterate-to-clean",
    ),
    pytest.param(
        False,
        [
            _OK,  # INIT -> REVIEW
            {"phase_success": False, "error_message": "review crashed"},  # fail 1
            {"phase_success": False, "error_message": "review crashed"},  # fail 2
            {
                "phase_success": False,
                "error_message": "review crashed",
            },  # fail 3 -> FAILED
        ],
        {
            "final_phase": EnumLocalReviewPhase.FAILED,
            "iteration_count": 1,
            "issues_found": 0,
            "issues_fixed": 0,
            "error": "review crashed",
        },
        id="circuit-breaker-FAILED-NEGATIVE",
    ),
    pytest.param(
        True,  # dry_run -> no-push semantics carried into state
        [
            _OK,  # INIT -> REVIEW
            _OK,  # REVIEW -> FIX
            _OK,  # FIX -> COMMIT
            _OK,  # COMMIT -> CHECK_CLEAN
            _OK_CLEAN,  # CHECK_CLEAN -> DONE
        ],
        {
            "final_phase": EnumLocalReviewPhase.DONE,
            "iteration_count": 1,
            "issues_found": 0,
            "issues_fixed": 0,
            "error": None,
        },
        id="dry-run-no-push-clean",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(("dry_run", "steps", "expected"), CASES)
def test_local_review_multiparam(
    dry_run: bool,
    steps: list[dict[str, Any]],
    expected: dict[str, Any],
) -> None:
    handler = HandlerLocalReview()
    correlation_id = uuid4()
    command = ModelLocalReviewStartCommand(
        correlation_id=correlation_id,
        dry_run=dry_run,
        max_iterations=10,
        required_clean_runs=1,
        requested_at=datetime.now(tz=UTC),
    )

    state, completed, _applied = _drive(handler, command, steps)

    assert state.current_phase == expected["final_phase"]
    assert completed.final_phase == expected["final_phase"]
    assert completed.correlation_id == correlation_id
    assert completed.iteration_count == expected["iteration_count"]
    assert completed.issues_found == expected["issues_found"]
    assert completed.issues_fixed == expected["issues_fixed"]
    assert completed.error_message == expected["error"]
    # dry_run is an immutable property of the run state (no-push assertion)
    assert state.dry_run is dry_run
    # completed_at must be at/after started_at (real timestamps, structural truth)
    assert completed.completed_at >= completed.started_at
