"""Golden chain tests for node_close_out.

Verifies the FSM state machine: start command -> phase transitions -> completion,
circuit breaker, dry_run, metrics accumulation, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_close_out.handlers.handler_close_out import (
    HandlerCloseOut,
)
from omnimarket.nodes.node_close_out.models.model_close_out_start_command import (
    ModelCloseOutStartCommand,
)
from omnimarket.nodes.node_close_out.models.model_close_out_state import (
    EnumCloseOutPhase,
)

CMD_TOPIC = "onex.cmd.omnimarket.close-out-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.close-out-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.close-out-completed.v1"


def _make_command(
    dry_run: bool = False,
) -> ModelCloseOutStartCommand:
    return ModelCloseOutStartCommand(
        correlation_id=uuid4(),
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestCloseOutGoldenChain:
    """Golden chain: start command -> phase transitions -> completion."""

    async def test_full_cycle_all_phases_succeed(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All 8 phases succeed -> DONE."""
        handler = HandlerCloseOut()
        command = _make_command()

        state, events, completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumCloseOutPhase.DONE
        assert state.consecutive_failures == 0
        assert state.error_message is None
        assert completed.final_phase == EnumCloseOutPhase.DONE

        # 8 transitions: IDLE->MERGE_SWEEP, MERGE_SWEEP->DEPLOY_PLUGIN,
        # DEPLOY_PLUGIN->START_ENV, START_ENV->INTEGRATION,
        # INTEGRATION->RELEASE_CHECK, RELEASE_CHECK->REDEPLOY_CHECK,
        # REDEPLOY_CHECK->DASHBOARD_SWEEP, DASHBOARD_SWEEP->DONE
        assert len(events) == 8
        assert all(e.success for e in events)
        assert events[0].from_phase == EnumCloseOutPhase.IDLE
        assert events[0].to_phase == EnumCloseOutPhase.MERGE_SWEEP
        assert events[-1].from_phase == EnumCloseOutPhase.DASHBOARD_SWEEP
        assert events[-1].to_phase == EnumCloseOutPhase.DONE

    async def test_circuit_breaker_after_3_failures(
        self, event_bus: EventBusInmemory
    ) -> None:
        """3 consecutive failures in the same phase -> FAILED."""
        handler = HandlerCloseOut()
        command = _make_command()
        state = handler.start(command)

        # IDLE -> MERGE_SWEEP (success)
        state, _ = handler.advance(state, phase_success=True)
        assert state.current_phase == EnumCloseOutPhase.MERGE_SWEEP

        # Fail MERGE_SWEEP 3 times
        state, _ = handler.advance(state, phase_success=False, error_message="fail 1")
        assert state.current_phase == EnumCloseOutPhase.MERGE_SWEEP
        assert state.consecutive_failures == 1

        state, _ = handler.advance(state, phase_success=False, error_message="fail 2")
        assert state.consecutive_failures == 2

        state, event3 = handler.advance(
            state, phase_success=False, error_message="fail 3"
        )
        assert state.current_phase == EnumCloseOutPhase.FAILED
        assert state.consecutive_failures == 3
        assert event3.to_phase == EnumCloseOutPhase.FAILED
        assert event3.success is False

    async def test_circuit_breaker_via_run_full_pipeline(
        self, event_bus: EventBusInmemory
    ) -> None:
        """run_full_pipeline with a failing phase breaks on first failure."""
        handler = HandlerCloseOut()
        command = _make_command()

        state, _events, completed = handler.run_full_pipeline(
            command,
            phase_results={EnumCloseOutPhase.INTEGRATION: False},
        )

        # After 1 failure trying to enter INTEGRATION, state stays at START_ENV
        assert completed.final_phase == EnumCloseOutPhase.START_ENV
        assert state.current_phase == EnumCloseOutPhase.START_ENV
        assert state.consecutive_failures == 1

    async def test_dry_run_propagated(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag propagates through state."""
        handler = HandlerCloseOut()
        command = _make_command(dry_run=True)

        state, _events, _completed = handler.run_full_pipeline(command)

        assert state.dry_run is True
        assert state.current_phase == EnumCloseOutPhase.DONE

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerCloseOut()
        completed_events: list[dict[str, object]] = []
        phase_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelCloseOutStartCommand(
                correlation_id=payload["correlation_id"],
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
            CMD_TOPIC, on_message=on_command, group_id="test-close-out"
        )

        cmd_payload = json.dumps({"correlation_id": str(uuid4())}).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["final_phase"] == "done"
        assert len(phase_events) == 8

        phase_history = await event_bus.get_event_history(topic=PHASE_TOPIC)
        assert len(phase_history) == 8

        completed_history = await event_bus.get_event_history(topic=COMPLETED_TOPIC)
        assert len(completed_history) == 1

        await event_bus.close()

    async def test_failure_resets_on_success(self, event_bus: EventBusInmemory) -> None:
        """A success after failures resets consecutive_failures to 0."""
        handler = HandlerCloseOut()
        command = _make_command()
        state = handler.start(command)

        # IDLE -> MERGE_SWEEP (success)
        state, _ = handler.advance(state, phase_success=True)

        # Fail twice
        state, _ = handler.advance(state, phase_success=False, error_message="fail 1")
        assert state.consecutive_failures == 1
        state, _ = handler.advance(state, phase_success=False, error_message="fail 2")
        assert state.consecutive_failures == 2

        # Succeed — resets counter and advances
        state, _ = handler.advance(state, phase_success=True)
        assert state.consecutive_failures == 0
        assert state.current_phase == EnumCloseOutPhase.DEPLOY_PLUGIN

    async def test_cannot_advance_from_terminal(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Advancing from DONE or FAILED raises ValueError."""
        handler = HandlerCloseOut()
        command = _make_command()

        state, _, _ = handler.run_full_pipeline(command)
        assert state.current_phase == EnumCloseOutPhase.DONE

        with pytest.raises(ValueError, match="terminal phase"):
            handler.advance(state, phase_success=True)

    async def test_phase_event_serialization(self, event_bus: EventBusInmemory) -> None:
        """Phase events serialize to valid JSON bytes."""
        handler = HandlerCloseOut()
        command = _make_command()
        state = handler.start(command)
        state, event = handler.advance(state, phase_success=True)

        serialized = handler.serialize_event(event)
        deserialized = json.loads(serialized)

        assert deserialized["from_phase"] == "idle"
        assert deserialized["to_phase"] == "merge_sweep"
        assert deserialized["success"] is True

    async def test_completed_event_serialization(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Completed events serialize to valid JSON bytes."""
        handler = HandlerCloseOut()
        command = _make_command()
        _, _, completed = handler.run_full_pipeline(command)

        serialized = handler.serialize_completed(completed)
        deserialized = json.loads(serialized)

        assert deserialized["final_phase"] == "done"

    async def test_metrics_accumulate(self, event_bus: EventBusInmemory) -> None:
        """PR metrics accumulate across phase transitions."""
        handler = HandlerCloseOut()
        command = _make_command()
        state = handler.start(command)

        # IDLE -> MERGE_SWEEP (with PRs merged)
        state, _ = handler.advance(
            state, phase_success=True, prs_merged=5, prs_polished=2
        )
        assert state.prs_merged == 5
        assert state.prs_polished == 2

        # MERGE_SWEEP -> DEPLOY_PLUGIN (more PRs)
        state, _ = handler.advance(
            state, phase_success=True, prs_merged=3, prs_polished=1
        )
        assert state.prs_merged == 8
        assert state.prs_polished == 3
