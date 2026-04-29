"""Golden chain tests for node_ticket_pipeline.

Verifies the FSM state machine: start command -> phase transitions -> completion,
circuit breaker, skip_test_iterate, dry_run, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

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
    skip_to: EnumPipelinePhase | None = None,
) -> ModelPipelineStartCommand:
    return ModelPipelineStartCommand(
        correlation_id=uuid4(),
        ticket_id=ticket_id,
        skip_test_iterate=skip_test_iterate,
        dry_run=dry_run,
        skip_to=skip_to,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestTicketPipelineGoldenChain:
    """Golden chain: start command -> phase transitions -> completion."""

    async def test_safe_slice_runs_preflight_then_blocks_implement(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Default execution is honest and stops at the first unwired phase."""
        handler = HandlerTicketPipeline()
        command = _make_command()

        report = handler.run_executable_pipeline(command)

        assert report.state.current_phase == EnumPipelinePhase.BLOCKED
        assert report.completed.final_phase == EnumPipelinePhase.BLOCKED
        assert report.completed.ticket_id == "OMN-9999"
        assert report.ran_phase == EnumPipelinePhase.IMPLEMENT
        assert report.stop_reason == "not_implemented"

        assert [result.phase for result in report.phase_results] == [
            EnumPipelinePhase.PRE_FLIGHT,
            EnumPipelinePhase.IMPLEMENT,
        ]
        assert report.phase_results[0].status == "succeeded"
        assert report.phase_results[1].status == "not_implemented"
        assert report.phase_events[0].from_phase == EnumPipelinePhase.PRE_FLIGHT
        assert report.phase_events[0].to_phase == EnumPipelinePhase.IMPLEMENT
        assert report.phase_events[-1].from_phase == EnumPipelinePhase.IMPLEMENT
        assert report.phase_events[-1].to_phase == EnumPipelinePhase.BLOCKED

    async def test_explicit_phase_results_can_drive_full_fsm(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Tests may still inject every phase result to exercise the FSM."""
        handler = HandlerTicketPipeline()
        command = _make_command()

        state, events, completed = handler.run_full_pipeline(
            command,
            phase_results={
                EnumPipelinePhase.PRE_FLIGHT: True,
                EnumPipelinePhase.IMPLEMENT: True,
                EnumPipelinePhase.LOCAL_REVIEW: True,
                EnumPipelinePhase.CREATE_PR: True,
                EnumPipelinePhase.TEST_ITERATE: True,
                EnumPipelinePhase.CI_WATCH: True,
                EnumPipelinePhase.PR_REVIEW: True,
                EnumPipelinePhase.AUTO_MERGE: True,
            },
        )

        assert state.current_phase == EnumPipelinePhase.DONE
        assert completed.final_phase == EnumPipelinePhase.DONE
        assert len(events) == 8
        assert all(e.success for e in events)

    async def test_skip_test_iterate(self, event_bus: EventBusInmemory) -> None:
        """skip_test_iterate=True skips TEST_ITERATE phase."""
        handler = HandlerTicketPipeline()
        command = _make_command(skip_test_iterate=True)

        state, events, _completed = handler.run_full_pipeline(
            command,
            phase_results={
                EnumPipelinePhase.PRE_FLIGHT: True,
                EnumPipelinePhase.IMPLEMENT: True,
                EnumPipelinePhase.LOCAL_REVIEW: True,
                EnumPipelinePhase.CREATE_PR: True,
                EnumPipelinePhase.CI_WATCH: True,
                EnumPipelinePhase.PR_REVIEW: True,
                EnumPipelinePhase.AUTO_MERGE: True,
            },
        )

        assert state.current_phase == EnumPipelinePhase.DONE
        assert len(events) == 7
        phase_names = [e.to_phase for e in events]
        assert EnumPipelinePhase.TEST_ITERATE not in phase_names

    async def test_circuit_breaker_after_3_failures(
        self, event_bus: EventBusInmemory
    ) -> None:
        """3 consecutive failures in the same phase -> FAILED."""
        handler = HandlerTicketPipeline()
        command = _make_command()
        state = handler.start(command)

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

        # PRE_FLIGHT runs, then the injected IMPLEMENT failure stops there.
        assert completed.final_phase == EnumPipelinePhase.IMPLEMENT
        assert state.consecutive_failures == 1

    async def test_dry_run_propagated(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag propagates through state."""
        handler = HandlerTicketPipeline()
        command = _make_command(dry_run=True)

        report = handler.run_executable_pipeline(command)

        assert report.state.dry_run is True
        assert report.phase_results[0].details["side_effects"] == "none"
        assert report.completed.final_phase == EnumPipelinePhase.BLOCKED

    async def test_skip_to_sets_initial_resume_phase(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler = HandlerTicketPipeline()
        command = _make_command(skip_to=EnumPipelinePhase.CREATE_PR)

        state = handler.start(command)
        report = handler.run_executable_pipeline(command)

        assert state.current_phase == EnumPipelinePhase.CREATE_PR
        assert report.phase_results[0].phase == EnumPipelinePhase.CREATE_PR
        assert report.stop_reason == "not_implemented"

    async def test_skip_to_rejects_terminal_phase(
        self, event_bus: EventBusInmemory
    ) -> None:
        with pytest.raises(ValidationError, match="skip_to"):
            _make_command(skip_to=EnumPipelinePhase.DONE)

    async def test_ticket_id_validation(self, event_bus: EventBusInmemory) -> None:
        with pytest.raises(ValidationError, match="ticket_id"):
            _make_command(ticket_id="omn-9999")

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
        assert completed_events[0]["final_phase"] == "blocked"
        assert len(phase_events) == 2

        phase_history = await event_bus.get_event_history(topic=PHASE_TOPIC)
        assert len(phase_history) == 2

        completed_history = await event_bus.get_event_history(topic=COMPLETED_TOPIC)
        assert len(completed_history) == 1

        await event_bus.close()

    async def test_failure_resets_on_success(self, event_bus: EventBusInmemory) -> None:
        """A success after failures resets consecutive_failures to 0."""
        handler = HandlerTicketPipeline()
        command = _make_command()
        state = handler.start(command)

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
        assert state.current_phase == EnumPipelinePhase.BLOCKED

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

        assert deserialized["from_phase"] == "pre_flight"
        assert deserialized["to_phase"] == "implement"
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

        assert deserialized["final_phase"] == "blocked"
        assert deserialized["ticket_id"] == "OMN-9999"

    async def test_pr_number_tracked(self, event_bus: EventBusInmemory) -> None:
        """PR number is tracked through state when provided during advance."""
        handler = HandlerTicketPipeline()
        command = _make_command()
        state = handler.start(command)

        # Advance to CREATE_PR phase
        for _ in range(3):  # PRE_FLIGHT->IMPLEMENT->LOCAL_REVIEW->CREATE_PR
            state, _ = handler.advance(state, phase_success=True)
        assert state.current_phase == EnumPipelinePhase.CREATE_PR

        # Advance from CREATE_PR with pr_number
        state, _ = handler.advance(state, phase_success=True, pr_number=42)
        assert state.pr_number == 42

    async def test_cli_emits_parseable_safe_execution_json(
        self, event_bus: EventBusInmemory
    ) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_ticket_pipeline",
                "OMN-9360",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0
        payload = json.loads(completed.stdout)
        assert payload["stopped_at"] == "blocked"
        assert payload["stop_reason"] == "not_implemented"
        assert [item["phase"] for item in payload["phase_results"]] == [
            "pre_flight",
            "implement",
        ]

    async def test_cli_skip_to_stops_at_requested_unwired_phase(
        self, event_bus: EventBusInmemory
    ) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_ticket_pipeline",
                "OMN-9360",
                "--skip-to",
                "create_pr",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0
        payload = json.loads(completed.stdout)
        assert payload["ran_phase"] == "create_pr"
        assert payload["phase_results"][0]["status"] == "not_implemented"
        assert payload["stopped_at"] == "blocked"
