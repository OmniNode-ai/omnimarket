"""Golden chain tests for node_redeploy.

Verifies the FSM state machine: start command -> phase transitions -> completion,
circuit breaker, version pins, dry_run, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_redeploy.handlers.handler_redeploy import (
    HandlerRedeploy,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    ModelRedeployCommand,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_state import (
    EnumRedeployPhase,
)

CMD_TOPIC = "onex.cmd.omnimarket.redeploy-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.redeploy-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.redeploy-completed.v1"


def _make_command(
    versions: dict[str, str] | None = None,
    skip_sync: bool = False,
    verify_only: bool = False,
    dry_run: bool = False,
) -> ModelRedeployCommand:
    return ModelRedeployCommand(
        correlation_id=uuid4(),
        versions=versions or {"omniintelligence": "0.8.0"},
        skip_sync=skip_sync,
        verify_only=verify_only,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestRedeployGoldenChain:
    """Golden chain: start command -> phase transitions -> completion."""

    async def test_full_cycle_all_phases_succeed(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All 5 phases succeed -> DONE."""
        handler = HandlerRedeploy()
        command = _make_command()

        state, events, completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumRedeployPhase.DONE
        assert completed.final_phase == EnumRedeployPhase.DONE
        # 6 transitions: IDLE->SYNC->UPDATE->REBUILD->SEED->VERIFY->DONE
        assert len(events) == 6
        assert all(e.success for e in events)
        assert events[0].from_phase == EnumRedeployPhase.IDLE
        assert events[0].to_phase == EnumRedeployPhase.SYNC_CLONES
        assert events[-1].to_phase == EnumRedeployPhase.DONE

    async def test_circuit_breaker_after_3_failures(
        self, event_bus: EventBusInmemory
    ) -> None:
        """3 consecutive failures -> FAILED."""
        handler = HandlerRedeploy()
        command = _make_command()
        state = handler.start(command)

        state, _ = handler.advance(state, phase_success=True)
        state, _ = handler.advance(state, phase_success=False, error_message="fail 1")
        state, _ = handler.advance(state, phase_success=False, error_message="fail 2")
        state, _ = handler.advance(state, phase_success=False, error_message="fail 3")

        assert state.current_phase == EnumRedeployPhase.FAILED

    async def test_versions_propagated(self, event_bus: EventBusInmemory) -> None:
        """Version pins propagate from command to state."""
        handler = HandlerRedeploy()
        command = _make_command(
            versions={"omniintelligence": "0.8.0", "omninode-claude": "0.4.0"}
        )

        state, _events, _completed = handler.run_full_pipeline(command)

        assert state.versions == {
            "omniintelligence": "0.8.0",
            "omninode-claude": "0.4.0",
        }

    async def test_phases_completed_counter(self, event_bus: EventBusInmemory) -> None:
        """phases_completed counter increments on each successful advance."""
        handler = HandlerRedeploy()
        command = _make_command()

        state, _events, completed = handler.run_full_pipeline(command)

        # 5 successful phases (SYNC, UPDATE, REBUILD, SEED, VERIFY) + DONE transition
        assert state.phases_completed == 6
        assert completed.phases_completed == 6

    async def test_dry_run_propagated(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag propagates through state."""
        handler = HandlerRedeploy()
        command = _make_command(dry_run=True)

        state, _events, _completed = handler.run_full_pipeline(command)
        assert state.dry_run is True

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerRedeploy()
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelRedeployCommand(
                correlation_id=payload["correlation_id"],
                versions=payload.get("versions", {}),
                dry_run=payload.get("dry_run", False),
                requested_at=datetime.now(tz=UTC),
            )
            _state, _events, completed = handler.run_full_pipeline(command)
            completed_payload = completed.model_dump(mode="json")
            completed_events.append(completed_payload)
            await event_bus.publish(
                COMPLETED_TOPIC,
                key=None,
                value=json.dumps(completed_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-redeploy"
        )

        cmd_payload = json.dumps(
            {
                "correlation_id": str(uuid4()),
                "versions": {"omniintelligence": "0.8.0"},
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["final_phase"] == "done"

        await event_bus.close()

    async def test_cannot_advance_from_terminal(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Advancing from DONE raises ValueError."""
        handler = HandlerRedeploy()
        command = _make_command()
        state, _, _ = handler.run_full_pipeline(command)

        with pytest.raises(ValueError, match="terminal phase"):
            handler.advance(state, phase_success=True)

    async def test_skip_sync_propagated(self, event_bus: EventBusInmemory) -> None:
        """skip_sync flag propagates from command to state."""
        handler = HandlerRedeploy()
        command = _make_command(skip_sync=True)
        state = handler.start(command)
        assert state.skip_sync is True

    async def test_phase_failure_stops_pipeline(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Pipeline stops on first failure."""
        handler = HandlerRedeploy()
        command = _make_command()

        state, _events, completed = handler.run_full_pipeline(
            command,
            phase_results={EnumRedeployPhase.REBUILD: False},
        )

        assert completed.final_phase == EnumRedeployPhase.UPDATE_PINS
        assert state.consecutive_failures == 1
