"""Golden chain tests for node_ticket_pipeline.

Verifies the FSM state machine: start command -> phase transitions -> completion,
circuit breaker, skip_test_iterate, dry_run, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_ticket_pipeline.handlers.handler_ticket_pipeline import (
    HandlerTicketPipeline,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_start_command import (
    ModelPipelineStartCommand,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_state import (
    EnumPipelinePhase,
)

CMD_TOPIC = "onex.cmd.omnimarket.ticket-pipeline-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.ticket-pipeline-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.ticket-pipeline-completed.v1"


def _make_command(
    ticket_id: str = "OMN-9999",
    skip_test_iterate: bool = False,
    dry_run: bool = False,
) -> ModelPipelineStartCommand:
    return ModelPipelineStartCommand(
        correlation_id=uuid4(),
        ticket_id=ticket_id,
        skip_test_iterate=skip_test_iterate,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestTicketPipelineGoldenChain:
    """Golden chain: start command -> phase transitions -> completion."""

    async def test_full_cycle_all_phases_succeed(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All phases succeed -> DONE."""
        handler = HandlerTicketPipeline()
        command = _make_command()

        state, events, completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumPipelinePhase.DONE
        assert state.consecutive_failures == 0
        assert state.error_message is None
        assert completed.final_phase == EnumPipelinePhase.DONE
        assert completed.ticket_id == "OMN-9999"

        # 9 transitions: IDLE->PRE_FLIGHT, PRE_FLIGHT->IMPLEMENT,
        # IMPLEMENT->LOCAL_REVIEW, LOCAL_REVIEW->CREATE_PR,
        # CREATE_PR->TEST_ITERATE, TEST_ITERATE->CI_WATCH,
        # CI_WATCH->PR_REVIEW, PR_REVIEW->AUTO_MERGE, AUTO_MERGE->DONE
        assert len(events) == 9
        assert all(e.success for e in events)
        assert events[0].from_phase == EnumPipelinePhase.IDLE
        assert events[0].to_phase == EnumPipelinePhase.PRE_FLIGHT
        assert events[-1].from_phase == EnumPipelinePhase.AUTO_MERGE
        assert events[-1].to_phase == EnumPipelinePhase.DONE

    async def test_skip_test_iterate(self, event_bus: EventBusInmemory) -> None:
        """skip_test_iterate=True skips TEST_ITERATE phase."""
        handler = HandlerTicketPipeline()
        command = _make_command(skip_test_iterate=True)

        state, events, _completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumPipelinePhase.DONE
        # 8 transitions (no TEST_ITERATE)
        assert len(events) == 8
        phase_names = [e.to_phase for e in events]
        assert EnumPipelinePhase.TEST_ITERATE not in phase_names

    async def test_circuit_breaker_after_3_failures(
        self, event_bus: EventBusInmemory
    ) -> None:
        """3 consecutive failures in the same phase -> FAILED."""
        handler = HandlerTicketPipeline()
        command = _make_command()
        state = handler.start(command)

        # IDLE -> PRE_FLIGHT (success)
        state, _event = handler.advance(state, phase_success=True)
        assert state.current_phase == EnumPipelinePhase.PRE_FLIGHT

        # Fail PRE_FLIGHT 3 times
        state, _ = handler.advance(state, phase_success=False, error_message="fail 1")
        assert state.current_phase == EnumPipelinePhase.PRE_FLIGHT
        assert state.consecutive_failures == 1

        state, _ = handler.advance(state, phase_success=False, error_message="fail 2")
        assert state.consecutive_failures == 2

        state, event3 = handler.advance(
            state, phase_success=False, error_message="fail 3"
        )
        assert state.current_phase == EnumPipelinePhase.FAILED
        assert state.consecutive_failures == 3
        assert event3.to_phase == EnumPipelinePhase.FAILED
        assert event3.success is False

    async def test_circuit_breaker_via_run_full_pipeline(
        self, event_bus: EventBusInmemory
    ) -> None:
        """run_full_pipeline with a failing phase breaks on first failure."""
        handler = HandlerTicketPipeline()
        command = _make_command()

        state, _events, completed = handler.run_full_pipeline(
            command,
            phase_results={EnumPipelinePhase.IMPLEMENT: False},
        )

        # After 1 failure, state stays at PRE_FLIGHT (phase before IMPLEMENT)
        assert completed.final_phase == EnumPipelinePhase.PRE_FLIGHT
        assert state.consecutive_failures == 1

    async def test_dry_run_propagated(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag propagates through state."""
        handler = HandlerTicketPipeline()
        command = _make_command(dry_run=True)

        state, _events, _completed = handler.run_full_pipeline(command)

        assert state.dry_run is True
        assert state.current_phase == EnumPipelinePhase.DONE

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerTicketPipeline()
        completed_events: list[dict[str, object]] = []
        phase_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelPipelineStartCommand(
                correlation_id=payload["correlation_id"],
                ticket_id=payload["ticket_id"],
                skip_test_iterate=payload.get("skip_test_iterate", False),
                dry_run=payload.get("dry_run", False),
                requested_at=datetime.now(tz=UTC),
            )
            _state, events, completed = handler.run_full_pipeline(command)

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
            CMD_TOPIC, on_message=on_command, group_id="test-ticket-pipeline"
        )

        cmd_payload = json.dumps(
            {
                "correlation_id": str(uuid4()),
                "ticket_id": "OMN-1234",
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["final_phase"] == "done"
        assert len(phase_events) == 9

        phase_history = await event_bus.get_event_history(topic=PHASE_TOPIC)
        assert len(phase_history) == 9

        completed_history = await event_bus.get_event_history(topic=COMPLETED_TOPIC)
        assert len(completed_history) == 1

        await event_bus.close()

    async def test_failure_resets_on_success(self, event_bus: EventBusInmemory) -> None:
        """A success after failures resets consecutive_failures to 0."""
        handler = HandlerTicketPipeline()
        command = _make_command()
        state = handler.start(command)

        # IDLE -> PRE_FLIGHT (success)
        state, _ = handler.advance(state, phase_success=True)

        # Fail twice
        state, _ = handler.advance(state, phase_success=False, error_message="fail 1")
        assert state.consecutive_failures == 1
        state, _ = handler.advance(state, phase_success=False, error_message="fail 2")
        assert state.consecutive_failures == 2

        # Succeed — resets counter and advances
        state, _ = handler.advance(state, phase_success=True)
        assert state.consecutive_failures == 0
        assert state.current_phase == EnumPipelinePhase.IMPLEMENT

    async def test_cannot_advance_from_terminal(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Advancing from DONE or FAILED raises ValueError."""
        handler = HandlerTicketPipeline()
        command = _make_command()

        state, _, _ = handler.run_full_pipeline(command)
        assert state.current_phase == EnumPipelinePhase.DONE

        with pytest.raises(ValueError, match="terminal phase"):
            handler.advance(state, phase_success=True)

    async def test_phase_event_serialization(self, event_bus: EventBusInmemory) -> None:
        """Phase events serialize to valid JSON bytes."""
        handler = HandlerTicketPipeline()
        command = _make_command()
        state = handler.start(command)
        state, event = handler.advance(state, phase_success=True)

        serialized = handler.serialize_event(event)
        deserialized = json.loads(serialized)

        assert deserialized["from_phase"] == "idle"
        assert deserialized["to_phase"] == "pre_flight"
        assert deserialized["success"] is True
        assert deserialized["ticket_id"] == "OMN-9999"

    async def test_completed_event_serialization(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Completed events serialize to valid JSON bytes."""
        handler = HandlerTicketPipeline()
        command = _make_command()
        _, _, completed = handler.run_full_pipeline(command)

        serialized = handler.serialize_completed(completed)
        deserialized = json.loads(serialized)

        assert deserialized["final_phase"] == "done"
        assert deserialized["ticket_id"] == "OMN-9999"

    async def test_pr_number_tracked(self, event_bus: EventBusInmemory) -> None:
        """PR number is tracked through state when provided during advance."""
        handler = HandlerTicketPipeline()
        command = _make_command()
        state = handler.start(command)

        # Advance to CREATE_PR phase
        for _ in range(4):  # IDLE->PRE_FLIGHT->IMPLEMENT->LOCAL_REVIEW->CREATE_PR
            state, _ = handler.advance(state, phase_success=True)
        assert state.current_phase == EnumPipelinePhase.CREATE_PR

        # Advance from CREATE_PR with pr_number
        state, _ = handler.advance(state, phase_success=True, pr_number=42)
        assert state.pr_number == 42
