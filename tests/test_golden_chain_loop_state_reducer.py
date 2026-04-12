# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_loop_state_reducer.

Verifies FSM transitions, circuit breaker, duplicate rejection,
terminal state handling, skip_closeout, and intent emission.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_loop_state_reducer.handlers.handler_loop_state import (
    HandlerLoopState,
)
from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_event import (
    EnumBuildLoopPhase,
    ModelBuildLoopEvent,
)
from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_intent import (
    EnumBuildLoopIntentType,
)
from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_state import (
    ModelBuildLoopState,
)

CMD_TOPIC = "onex.cmd.omnimarket.loop-state-reduce.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.loop-state-reduced.v1"


def _state(
    phase: EnumBuildLoopPhase = EnumBuildLoopPhase.IDLE,
    **kwargs: object,
) -> ModelBuildLoopState:
    cid = kwargs.pop("correlation_id", uuid4())  # type: ignore[arg-type]
    return ModelBuildLoopState(correlation_id=cid, phase=phase, **kwargs)  # type: ignore[arg-type]


def _event(
    source_phase: EnumBuildLoopPhase,
    correlation_id: object,
    success: bool = True,
    error_message: str | None = None,
    **kwargs: object,
) -> ModelBuildLoopEvent:
    return ModelBuildLoopEvent(
        correlation_id=correlation_id,  # type: ignore[arg-type]
        source_phase=source_phase,
        success=success,
        timestamp=datetime.now(tz=UTC),
        error_message=error_message,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.unit
class TestLoopStateReducerGoldenChain:
    """Golden chain: state + event -> new_state + intents."""

    async def test_idle_to_closing_out(self, event_bus: EventBusInmemory) -> None:
        """IDLE + success -> CLOSING_OUT with START_CLOSEOUT intent."""
        handler = HandlerLoopState()
        state = _state()
        event = _event(EnumBuildLoopPhase.IDLE, state.correlation_id)

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumBuildLoopPhase.CLOSING_OUT
        assert new_state.cycle_number == 1
        assert new_state.consecutive_failures == 0
        assert len(intents) == 1
        assert intents[0].intent_type == EnumBuildLoopIntentType.START_CLOSEOUT

    async def test_full_happy_path(self, event_bus: EventBusInmemory) -> None:
        """Walk through IDLE -> CLOSING_OUT -> ... -> COMPLETE."""
        handler = HandlerLoopState()
        state = _state()
        cid = state.correlation_id

        phases = [
            (EnumBuildLoopPhase.IDLE, EnumBuildLoopPhase.CLOSING_OUT),
            (EnumBuildLoopPhase.CLOSING_OUT, EnumBuildLoopPhase.VERIFYING),
            (EnumBuildLoopPhase.VERIFYING, EnumBuildLoopPhase.FILLING),
            (EnumBuildLoopPhase.FILLING, EnumBuildLoopPhase.CLASSIFYING),
            (EnumBuildLoopPhase.CLASSIFYING, EnumBuildLoopPhase.BUILDING),
            (EnumBuildLoopPhase.BUILDING, EnumBuildLoopPhase.COMPLETE),
        ]

        for from_phase, expected_phase in phases:
            event = _event(from_phase, cid)
            state, intents = handler.delta(state, event)
            assert state.phase == expected_phase
            assert len(intents) == 1

    async def test_failure_increments_counter(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Failed event increments consecutive_failures."""
        handler = HandlerLoopState()
        state = _state()
        event = _event(
            EnumBuildLoopPhase.IDLE,
            state.correlation_id,
            success=False,
            error_message="closeout failed",
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumBuildLoopPhase.FAILED
        assert new_state.consecutive_failures == 1
        assert len(intents) == 0

    async def test_circuit_breaker_trips(self, event_bus: EventBusInmemory) -> None:
        """After max_consecutive_failures, circuit breaker emits CIRCUIT_BREAK."""
        handler = HandlerLoopState()
        cid = uuid4()
        state = _state(
            correlation_id=cid,
            phase=EnumBuildLoopPhase.CLOSING_OUT,
            consecutive_failures=2,
            max_consecutive_failures=3,
        )
        event = _event(
            EnumBuildLoopPhase.CLOSING_OUT,
            cid,
            success=False,
            error_message="third failure",
        )

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumBuildLoopPhase.FAILED
        assert new_state.consecutive_failures == 3
        assert len(intents) == 1
        assert intents[0].intent_type == EnumBuildLoopIntentType.CIRCUIT_BREAK

    async def test_duplicate_correlation_rejected(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Event with wrong correlation_id is rejected."""
        handler = HandlerLoopState()
        state = _state()
        event = _event(EnumBuildLoopPhase.IDLE, uuid4())  # different cid

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumBuildLoopPhase.IDLE  # unchanged
        assert len(intents) == 0

    async def test_out_of_order_event_rejected(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Event with wrong source_phase is rejected."""
        handler = HandlerLoopState()
        state = _state()
        event = _event(EnumBuildLoopPhase.BUILDING, state.correlation_id)  # wrong phase

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumBuildLoopPhase.IDLE  # unchanged
        assert len(intents) == 0

    async def test_terminal_state_rejects_events(
        self, event_bus: EventBusInmemory
    ) -> None:
        """COMPLETE and FAILED states reject all events."""
        handler = HandlerLoopState()
        cid = uuid4()

        for terminal in (EnumBuildLoopPhase.COMPLETE, EnumBuildLoopPhase.FAILED):
            state = _state(phase=terminal, correlation_id=cid)
            event = _event(terminal, cid)
            new_state, intents = handler.delta(state, event)
            assert new_state.phase == terminal
            assert len(intents) == 0

    async def test_skip_closeout(self, event_bus: EventBusInmemory) -> None:
        """skip_closeout=True skips CLOSING_OUT, goes IDLE -> VERIFYING."""
        handler = HandlerLoopState()
        state = _state(skip_closeout=True)
        event = _event(EnumBuildLoopPhase.IDLE, state.correlation_id)

        new_state, intents = handler.delta(state, event)

        assert new_state.phase == EnumBuildLoopPhase.VERIFYING
        assert len(intents) == 1
        assert intents[0].intent_type == EnumBuildLoopIntentType.START_VERIFY

    async def test_tickets_filled_captured(self, event_bus: EventBusInmemory) -> None:
        """tickets_filled from event is captured in state."""
        handler = HandlerLoopState()
        cid = uuid4()
        state = _state(phase=EnumBuildLoopPhase.VERIFYING, correlation_id=cid)
        event = _event(EnumBuildLoopPhase.VERIFYING, cid, tickets_filled=5)

        new_state, _intents = handler.delta(state, event)

        assert new_state.phase == EnumBuildLoopPhase.FILLING
        assert new_state.tickets_filled == 5

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerLoopState()
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            result = handler.handle(payload)
            completed_events.append(result)
            await event_bus.publish(
                COMPLETED_TOPIC,
                key=None,
                value=json.dumps(result).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-loop-state"
        )

        cid = str(uuid4())
        cmd_payload = json.dumps(
            {
                "state": {
                    "correlation_id": cid,
                    "phase": "idle",
                },
                "event": {
                    "correlation_id": cid,
                    "source_phase": "idle",
                    "success": True,
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                },
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["state"]["phase"] == "closing_out"
        assert len(completed_events[0]["intents"]) == 1

        await event_bus.close()
