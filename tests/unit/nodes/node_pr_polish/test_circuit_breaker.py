# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three-failure circuit breaker of node_pr_polish, end to end through the
handler's events: the phase-transition event that trips it, the completed event
that carries it, and the counter reset a success gives."""

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
    ModelPrPolishState,
)


def _started() -> tuple[HandlerPrPolish, ModelPrPolishState]:
    handler = HandlerPrPolish()
    state = handler.start(
        ModelPrPolishStartCommand(
            correlation_id=uuid4(), pr_number=7, requested_at=datetime.now(tz=UTC)
        )
    )
    state, _ = handler.advance(state, phase_success=True)
    assert state.current_phase == EnumPrPolishPhase.RESOLVE_CONFLICTS
    return handler, state


@pytest.mark.unit
def test_third_consecutive_failure_trips_breaker_and_completes_failed() -> None:
    handler, state = _started()
    started_at = datetime.now(tz=UTC)

    events = []
    for attempt in (1, 2):
        state, event = handler.advance(
            state, phase_success=False, error_message=f"ci red {attempt}"
        )
        events.append(event)
        # Positive control: below the limit the phase is held, not failed.
        assert state.current_phase == EnumPrPolishPhase.RESOLVE_CONFLICTS
        assert event.to_phase == EnumPrPolishPhase.RESOLVE_CONFLICTS
        assert state.consecutive_failures == attempt

    state, tripped = handler.advance(
        state, phase_success=False, error_message="ci red 3"
    )

    assert state.current_phase == EnumPrPolishPhase.FAILED
    assert state.consecutive_failures == 3
    assert tripped.from_phase == EnumPrPolishPhase.RESOLVE_CONFLICTS
    assert tripped.to_phase == EnumPrPolishPhase.FAILED
    assert tripped.success is False

    completed = handler.make_completed_event(state, started_at)
    assert completed.final_phase == EnumPrPolishPhase.FAILED
    assert completed.error_message == "ci red 3"
    assert completed.pr_number == 7

    # A tripped run is terminal: it cannot be advanced again.
    with pytest.raises(ValueError, match="terminal"):
        handler.advance(state, phase_success=True)


@pytest.mark.unit
def test_a_success_resets_the_failure_count() -> None:
    handler, state = _started()

    state, _ = handler.advance(state, phase_success=False, error_message="one")
    state, _ = handler.advance(state, phase_success=False, error_message="two")
    assert state.consecutive_failures == 2

    state, event = handler.advance(state, phase_success=True)
    assert state.consecutive_failures == 0
    assert state.error_message is None
    assert event.to_phase == EnumPrPolishPhase.FIX_CI

    state, _ = handler.advance(state, phase_success=False, error_message="three")
    state, _ = handler.advance(state, phase_success=False, error_message="four")
    assert state.current_phase == EnumPrPolishPhase.FIX_CI
    assert state.consecutive_failures == 2
