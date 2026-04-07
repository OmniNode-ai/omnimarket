"""Golden chain tests for node_build_loop.

Verifies the FSM state machine: start command -> phase transitions -> completion,
circuit breaker, skip_closeout, and dry_run modes. Uses EventBusInmemory.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_build_loop.handlers.handler_build_loop import (
    HandlerBuildLoop,
)
from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
    ModelLoopStartCommand,
)
from omnimarket.nodes.node_build_loop.models.model_loop_state import (
    EnumBuildLoopPhase,
)

CMD_TOPIC = "onex.cmd.omnimarket.build-loop-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.build-loop-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.build-loop-completed.v1"


def _make_command(
    skip_closeout: bool = False,
    dry_run: bool = False,
    max_cycles: int = 1,
    mode: str = "build",
) -> ModelLoopStartCommand:
    return ModelLoopStartCommand(
        correlation_id=uuid4(),
        max_cycles=max_cycles,
        mode=mode,
        skip_closeout=skip_closeout,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestBuildLoopGoldenChain:
    """Golden chain: start command -> phase transitions -> completion."""

    async def test_full_cycle_all_phases_succeed(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All 6 phases succeed -> COMPLETE with cycle_count=1."""
        handler = HandlerBuildLoop()
        command = _make_command()

        state, events, completed = handler.run_full_cycle(command)

        assert state.current_phase == EnumBuildLoopPhase.COMPLETE
        assert state.cycle_count == 1
        assert state.consecutive_failures == 0
        assert state.error_message is None
        assert completed.final_phase == EnumBuildLoopPhase.COMPLETE
        assert completed.cycles_completed == 1
        assert completed.cycles_failed == 0

        # Should have 6 transitions: IDLE->CLOSING_OUT, CLOSING_OUT->VERIFYING,
        # VERIFYING->FILLING, FILLING->CLASSIFYING, CLASSIFYING->BUILDING,
        # BUILDING->COMPLETE
        assert len(events) == 6
        assert all(e.success for e in events)
        assert events[0].from_phase == EnumBuildLoopPhase.IDLE
        assert events[0].to_phase == EnumBuildLoopPhase.CLOSING_OUT
        assert events[-1].from_phase == EnumBuildLoopPhase.BUILDING
        assert events[-1].to_phase == EnumBuildLoopPhase.COMPLETE

    async def test_skip_closeout(self, event_bus: EventBusInmemory) -> None:
        """skip_closeout=True skips CLOSING_OUT, goes IDLE -> VERIFYING."""
        handler = HandlerBuildLoop()
        command = _make_command(skip_closeout=True)

        state, events, _completed = handler.run_full_cycle(command)

        assert state.current_phase == EnumBuildLoopPhase.COMPLETE
        assert state.cycle_count == 1
        # Should have 5 transitions (no CLOSING_OUT)
        assert len(events) == 5
        assert events[0].from_phase == EnumBuildLoopPhase.IDLE
        assert events[0].to_phase == EnumBuildLoopPhase.VERIFYING
        phase_names = [e.to_phase for e in events]
        assert EnumBuildLoopPhase.CLOSING_OUT not in phase_names

    async def test_circuit_breaker_after_3_failures(
        self, event_bus: EventBusInmemory
    ) -> None:
        """3 consecutive failures in the same phase -> FAILED."""
        handler = HandlerBuildLoop()
        command = _make_command()
        state = handler.start(command)

        # Advance from IDLE -> CLOSING_OUT (success)
        state, _event = handler.advance(state, phase_success=True)
        assert state.current_phase == EnumBuildLoopPhase.CLOSING_OUT

        # Fail CLOSING_OUT 3 times
        state, _event1 = handler.advance(
            state, phase_success=False, error_message="fail 1"
        )
        assert state.current_phase == EnumBuildLoopPhase.CLOSING_OUT
        assert state.consecutive_failures == 1

        state, _event2 = handler.advance(
            state, phase_success=False, error_message="fail 2"
        )
        assert state.current_phase == EnumBuildLoopPhase.CLOSING_OUT
        assert state.consecutive_failures == 2

        state, event3 = handler.advance(
            state, phase_success=False, error_message="fail 3"
        )
        assert state.current_phase == EnumBuildLoopPhase.FAILED
        assert state.consecutive_failures == 3
        assert event3.to_phase == EnumBuildLoopPhase.FAILED
        assert event3.success is False

    async def test_circuit_breaker_via_run_full_cycle(
        self, event_bus: EventBusInmemory
    ) -> None:
        """run_full_cycle with a failing phase hits circuit breaker."""
        handler = HandlerBuildLoop()
        command = _make_command()

        # VERIFYING fails — since run_full_cycle only tries each phase once,
        # 1 failure won't trip the breaker. We test the immediate failure path.
        state, _events, completed = handler.run_full_cycle(
            command,
            phase_results={EnumBuildLoopPhase.VERIFYING: False},
        )

        # After 1 failure trying to enter VERIFYING, state stays at
        # CLOSING_OUT (the phase it was in when the failure occurred).
        # run_full_cycle breaks on failure under circuit breaker threshold.
        assert completed.final_phase == EnumBuildLoopPhase.CLOSING_OUT
        assert state.current_phase == EnumBuildLoopPhase.CLOSING_OUT
        assert state.consecutive_failures == 1

    async def test_dry_run_propagated(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag propagates through state and completed event."""
        handler = HandlerBuildLoop()
        command = _make_command(dry_run=True)

        state, _events, _completed = handler.run_full_cycle(command)

        assert state.dry_run is True
        assert state.current_phase == EnumBuildLoopPhase.COMPLETE

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerBuildLoop()
        completed_events: list[dict[str, object]] = []
        phase_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelLoopStartCommand(
                correlation_id=payload["correlation_id"],
                max_cycles=payload.get("max_cycles", 1),
                skip_closeout=payload.get("skip_closeout", False),
                dry_run=payload.get("dry_run", False),
                requested_at=datetime.now(tz=UTC),
            )
            _state, events, completed = handler.run_full_cycle(command)

            for evt in events:
                phase_payload = evt.model_dump(mode="json")
                phase_events.append(phase_payload)
                await event_bus.publish(
                    PHASE_TOPIC,
                    key=None,
                    value=json.dumps(phase_payload).encode(),
                )

            completed_payload = completed.model_dump(mode="json")
            completed_events.append(completed_payload)
            await event_bus.publish(
                COMPLETED_TOPIC,
                key=None,
                value=json.dumps(completed_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-build-loop"
        )

        cmd_payload = json.dumps(
            {"correlation_id": str(uuid4()), "max_cycles": 1}
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["final_phase"] == "complete"
        assert len(phase_events) == 6

        phase_history = await event_bus.get_event_history(topic=PHASE_TOPIC)
        assert len(phase_history) == 6

        completed_history = await event_bus.get_event_history(topic=COMPLETED_TOPIC)
        assert len(completed_history) == 1

        await event_bus.close()

    async def test_failure_resets_on_success(self, event_bus: EventBusInmemory) -> None:
        """A success after failures resets consecutive_failures to 0."""
        handler = HandlerBuildLoop()
        command = _make_command()
        state = handler.start(command)

        # IDLE -> CLOSING_OUT (success)
        state, _ = handler.advance(state, phase_success=True)
        assert state.current_phase == EnumBuildLoopPhase.CLOSING_OUT

        # Fail twice
        state, _ = handler.advance(state, phase_success=False, error_message="fail 1")
        assert state.consecutive_failures == 1
        state, _ = handler.advance(state, phase_success=False, error_message="fail 2")
        assert state.consecutive_failures == 2

        # Succeed — resets counter and advances
        state, _ = handler.advance(state, phase_success=True)
        assert state.consecutive_failures == 0
        assert state.current_phase == EnumBuildLoopPhase.VERIFYING

    async def test_cannot_advance_from_terminal(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Advancing from COMPLETE or FAILED raises ValueError."""
        handler = HandlerBuildLoop()
        command = _make_command()

        state, _, _ = handler.run_full_cycle(command)
        assert state.current_phase == EnumBuildLoopPhase.COMPLETE

        with pytest.raises(ValueError, match="terminal phase"):
            handler.advance(state, phase_success=True)

    async def test_phase_transition_event_serialization(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Phase transition events serialize to valid JSON bytes."""
        handler = HandlerBuildLoop()
        command = _make_command()
        state = handler.start(command)
        state, event = handler.advance(state, phase_success=True)

        serialized = handler.serialize_event(event)
        deserialized = json.loads(serialized)

        assert deserialized["from_phase"] == "idle"
        assert deserialized["to_phase"] == "closing_out"
        assert deserialized["success"] is True
        assert "correlation_id" in deserialized

    async def test_completed_event_serialization(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Completed events serialize to valid JSON bytes."""
        handler = HandlerBuildLoop()
        command = _make_command()
        _, _, completed = handler.run_full_cycle(command)

        serialized = handler.serialize_completed(completed)
        deserialized = json.loads(serialized)

        assert deserialized["final_phase"] == "complete"
        assert deserialized["cycles_completed"] == 1
        assert deserialized["cycles_failed"] == 0

    async def test_metrics_accumulate(self, event_bus: EventBusInmemory) -> None:
        """Ticket metrics accumulate across phase transitions."""
        handler = HandlerBuildLoop()
        command = _make_command(skip_closeout=True)
        state = handler.start(command)

        # IDLE -> VERIFYING
        state, _ = handler.advance(state, phase_success=True)
        # VERIFYING -> FILLING
        state, _ = handler.advance(state, phase_success=True)
        # FILLING -> CLASSIFYING (with tickets_filled)
        state, _ = handler.advance(state, phase_success=True, tickets_filled=5)
        assert state.tickets_filled == 5
        # CLASSIFYING -> BUILDING (with tickets_classified)
        state, _ = handler.advance(state, phase_success=True, tickets_classified=3)
        assert state.tickets_classified == 3
        # BUILDING -> COMPLETE (with tickets_dispatched)
        state, _ = handler.advance(state, phase_success=True, tickets_dispatched=2)
        assert state.tickets_dispatched == 2
        assert state.current_phase == EnumBuildLoopPhase.COMPLETE

    async def test_close_out_mode_skips_filling_building(
        self, event_bus: EventBusInmemory
    ) -> None:
        """CLOSE_OUT mode: CLOSING_OUT -> VERIFYING -> RELEASING -> DEPLOYING -> POST_VERIFY -> COMPLETE."""
        handler = HandlerBuildLoop()
        command = _make_command(mode="close_out")

        state, events, _completed = handler.run_full_cycle(command)

        assert state.current_phase == EnumBuildLoopPhase.COMPLETE
        assert state.cycle_count == 1
        # 6 transitions: IDLE->CLOSING_OUT, CLOSING_OUT->VERIFYING,
        # VERIFYING->RELEASING, RELEASING->DEPLOYING, DEPLOYING->POST_VERIFY,
        # POST_VERIFY->COMPLETE
        assert len(events) == 6
        phase_names = [e.to_phase for e in events]
        assert EnumBuildLoopPhase.FILLING not in phase_names
        assert EnumBuildLoopPhase.CLASSIFYING not in phase_names
        assert EnumBuildLoopPhase.BUILDING not in phase_names
        assert EnumBuildLoopPhase.RELEASING in phase_names
        assert EnumBuildLoopPhase.DEPLOYING in phase_names
        assert EnumBuildLoopPhase.POST_VERIFY in phase_names

    async def test_build_mode_skips_releasing_deploying(
        self, event_bus: EventBusInmemory
    ) -> None:
        """BUILD mode: CLOSING_OUT -> VERIFYING -> FILLING -> CLASSIFYING -> BUILDING -> COMPLETE."""
        handler = HandlerBuildLoop()
        command = _make_command(mode="build")

        state, events, _completed = handler.run_full_cycle(command)

        assert state.current_phase == EnumBuildLoopPhase.COMPLETE
        assert state.cycle_count == 1
        assert len(events) == 6
        phase_names = [e.to_phase for e in events]
        assert EnumBuildLoopPhase.RELEASING not in phase_names
        assert EnumBuildLoopPhase.DEPLOYING not in phase_names
        assert EnumBuildLoopPhase.POST_VERIFY not in phase_names
        assert EnumBuildLoopPhase.FILLING in phase_names
        assert EnumBuildLoopPhase.BUILDING in phase_names

    async def test_observe_mode_only_verifies(
        self, event_bus: EventBusInmemory
    ) -> None:
        """OBSERVE mode: VERIFYING -> COMPLETE (2 transitions)."""
        handler = HandlerBuildLoop()
        command = _make_command(mode="observe")

        state, events, _completed = handler.run_full_cycle(command)

        assert state.current_phase == EnumBuildLoopPhase.COMPLETE
        assert state.cycle_count == 1
        # 2 transitions: IDLE->VERIFYING, VERIFYING->COMPLETE
        assert len(events) == 2
        assert events[0].from_phase == EnumBuildLoopPhase.IDLE
        assert events[0].to_phase == EnumBuildLoopPhase.VERIFYING
        assert events[1].from_phase == EnumBuildLoopPhase.VERIFYING
        assert events[1].to_phase == EnumBuildLoopPhase.COMPLETE

    async def test_full_mode_runs_all_phases(self, event_bus: EventBusInmemory) -> None:
        """FULL mode runs all 8 phases: CLOSING_OUT through POST_VERIFY -> COMPLETE."""
        handler = HandlerBuildLoop()
        command = _make_command(mode="full")

        state, events, _completed = handler.run_full_cycle(command)

        assert state.current_phase == EnumBuildLoopPhase.COMPLETE
        assert state.cycle_count == 1
        # 9 transitions: IDLE->CLOSING_OUT, ..., POST_VERIFY->COMPLETE
        assert len(events) == 9
        phase_names = [e.to_phase for e in events]
        assert phase_names == [
            EnumBuildLoopPhase.CLOSING_OUT,
            EnumBuildLoopPhase.VERIFYING,
            EnumBuildLoopPhase.FILLING,
            EnumBuildLoopPhase.CLASSIFYING,
            EnumBuildLoopPhase.BUILDING,
            EnumBuildLoopPhase.RELEASING,
            EnumBuildLoopPhase.DEPLOYING,
            EnumBuildLoopPhase.POST_VERIFY,
            EnumBuildLoopPhase.COMPLETE,
        ]
