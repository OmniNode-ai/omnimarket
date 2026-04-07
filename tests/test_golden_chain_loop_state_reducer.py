"""Golden chain tests for node_loop_state_reducer.

Verifies the FSM delta function: state + event -> (new_state, intents).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.nodes.node_build_loop.models.model_loop_state import (
    EnumBuildLoopPhase,
)
from omnimarket.nodes.node_loop_state_reducer.handlers.handler_loop_state import (
    HandlerLoopState,
)
from omnimarket.nodes.node_loop_state_reducer.models.enum_build_loop_intent_type import (
    EnumBuildLoopIntentType,
)
from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_event import (
    ModelBuildLoopEvent,
)
from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_state import (
    ModelBuildLoopState,
)


def _state(
    phase: EnumBuildLoopPhase = EnumBuildLoopPhase.IDLE,
    correlation_id: str | None = None,
    **kwargs: object,
) -> ModelBuildLoopState:
    cid = correlation_id or str(uuid4())
    return ModelBuildLoopState(
        correlation_id=cid,  # type: ignore[arg-type]
        phase=phase,
        **kwargs,
    )


def _event(
    state: ModelBuildLoopState,
    success: bool = True,
    error_message: str | None = None,
    **kwargs: object,
) -> ModelBuildLoopEvent:
    return ModelBuildLoopEvent(
        correlation_id=state.correlation_id,
        source_phase=state.phase,
        success=success,
        timestamp=datetime.now(tz=UTC),
        error_message=error_message,
        **kwargs,
    )


@pytest.mark.unit
class TestLoopStateReducerGoldenChain:
    """Golden chain: delta(state, event) -> (new_state, intents)."""

    def test_idle_to_closing_out(self) -> None:
        """Success from IDLE -> CLOSING_OUT with START_CLOSEOUT intent."""
        handler = HandlerLoopState()
        state = _state(EnumBuildLoopPhase.IDLE)
        event = _event(state)

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumBuildLoopPhase.CLOSING_OUT
        assert new_state.consecutive_failures == 0
        assert len(intents) == 1
        assert intents[0].intent_type == EnumBuildLoopIntentType.START_CLOSEOUT

    def test_skip_closeout(self) -> None:
        """IDLE with skip_closeout -> VERIFYING directly."""
        handler = HandlerLoopState()
        state = _state(EnumBuildLoopPhase.IDLE, skip_closeout=True)
        event = _event(state)

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumBuildLoopPhase.VERIFYING
        assert len(intents) == 1
        assert intents[0].intent_type == EnumBuildLoopIntentType.START_VERIFY

    def test_failure_increments_counter(self) -> None:
        """Failure increments consecutive_failures."""
        handler = HandlerLoopState()
        state = _state(EnumBuildLoopPhase.CLOSING_OUT)
        event = _event(state, success=False, error_message="test error")

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumBuildLoopPhase.FAILED
        assert new_state.consecutive_failures == 1
        assert new_state.error_message == "test error"
        assert len(intents) == 0

    def test_circuit_breaker_trips(self) -> None:
        """3 consecutive failures -> FAILED + CIRCUIT_BREAK intent."""
        handler = HandlerLoopState()
        state = _state(
            EnumBuildLoopPhase.VERIFYING,
            consecutive_failures=2,
            max_consecutive_failures=3,
        )
        event = _event(state, success=False, error_message="final fail")

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumBuildLoopPhase.FAILED
        assert new_state.consecutive_failures == 3
        assert len(intents) == 1
        assert intents[0].intent_type == EnumBuildLoopIntentType.CIRCUIT_BREAK

    def test_rejects_correlation_mismatch(self) -> None:
        """Events with wrong correlation_id are rejected."""
        handler = HandlerLoopState()
        state = _state(EnumBuildLoopPhase.CLOSING_OUT)
        event = ModelBuildLoopEvent(
            correlation_id=uuid4(),  # different from state
            source_phase=EnumBuildLoopPhase.CLOSING_OUT,
            success=True,
            timestamp=datetime.now(tz=UTC),
        )

        new_state, intents = handler.delta(state, event)

        assert new_state is state  # unchanged
        assert len(intents) == 0

    def test_rejects_out_of_order_event(self) -> None:
        """Events from wrong phase are rejected."""
        handler = HandlerLoopState()
        state = _state(EnumBuildLoopPhase.CLOSING_OUT)
        event = ModelBuildLoopEvent(
            correlation_id=state.correlation_id,
            source_phase=EnumBuildLoopPhase.FILLING,  # wrong phase
            success=True,
            timestamp=datetime.now(tz=UTC),
        )

        new_state, intents = handler.delta(state, event)

        assert new_state is state
        assert len(intents) == 0

    def test_rejects_terminal_state(self) -> None:
        """Events in terminal states are rejected."""
        handler = HandlerLoopState()
        state = _state(EnumBuildLoopPhase.COMPLETE)
        event = _event(state)

        new_state, intents = handler.delta(state, event)

        assert new_state is state
        assert len(intents) == 0

    def test_building_to_complete(self) -> None:
        """BUILDING success -> COMPLETE with CYCLE_COMPLETE intent."""
        handler = HandlerLoopState()
        state = _state(EnumBuildLoopPhase.BUILDING)
        event = _event(state, tickets_dispatched=3)

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumBuildLoopPhase.COMPLETE
        assert new_state.tickets_dispatched == 3
        assert len(intents) == 1
        assert intents[0].intent_type == EnumBuildLoopIntentType.CYCLE_COMPLETE

    def test_captures_phase_metrics(self) -> None:
        """Ticket counts from events are captured in state."""
        handler = HandlerLoopState()
        state = _state(EnumBuildLoopPhase.FILLING)
        event = _event(state, tickets_filled=5)

        new_state, _ = handler.delta(state, event)

        assert new_state.tickets_filled == 5
