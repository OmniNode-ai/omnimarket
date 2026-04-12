"""Golden chain tests for node_design_to_plan.

Verifies the FSM state machine: start command -> phase transitions -> completion,
circuit breaker, dry_run, plan_path propagation, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_design_to_plan.handlers.handler_design_to_plan import (
    HandlerDesignToPlan,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_command import (
    ModelDesignToPlanCommand,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_state import (
    EnumDesignToPlanPhase,
)

CMD_TOPIC = "onex.cmd.omnimarket.design-to-plan-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.design-to-plan-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.design-to-plan-completed.v1"


def _make_command(
    topic: str = "Build a new dashboard",
    dry_run: bool = False,
    no_launch: bool = False,
) -> ModelDesignToPlanCommand:
    return ModelDesignToPlanCommand(
        correlation_id=uuid4(),
        topic=topic,
        no_launch=no_launch,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestDesignToPlanGoldenChain:
    """Golden chain: start command -> phase transitions -> completion."""

    async def test_full_cycle_all_phases_succeed(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All 4 phases succeed -> DONE."""
        handler = HandlerDesignToPlan()
        command = _make_command()

        state, events, completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumDesignToPlanPhase.DONE
        assert state.consecutive_failures == 0
        assert state.error_message is None
        assert completed.final_phase == EnumDesignToPlanPhase.DONE
        # 4 transitions: IDLE->BRAINSTORM, BRAINSTORM->STRUCTURE,
        # STRUCTURE->REVIEW, REVIEW->FINALIZE, FINALIZE->DONE = 5
        assert len(events) == 5
        assert all(e.success for e in events)
        assert events[0].from_phase == EnumDesignToPlanPhase.IDLE
        assert events[0].to_phase == EnumDesignToPlanPhase.BRAINSTORM
        assert events[-1].from_phase == EnumDesignToPlanPhase.FINALIZE
        assert events[-1].to_phase == EnumDesignToPlanPhase.DONE

    async def test_circuit_breaker_after_3_failures(
        self, event_bus: EventBusInmemory
    ) -> None:
        """3 consecutive failures in the same phase -> FAILED."""
        handler = HandlerDesignToPlan()
        command = _make_command()
        state = handler.start(command)

        # IDLE -> BRAINSTORM (success)
        state, _ = handler.advance(state, phase_success=True)
        assert state.current_phase == EnumDesignToPlanPhase.BRAINSTORM

        # Fail BRAINSTORM 3 times
        state, _ = handler.advance(state, phase_success=False, error_message="fail 1")
        assert state.consecutive_failures == 1
        state, _ = handler.advance(state, phase_success=False, error_message="fail 2")
        assert state.consecutive_failures == 2
        state, event3 = handler.advance(
            state, phase_success=False, error_message="fail 3"
        )
        assert state.current_phase == EnumDesignToPlanPhase.FAILED
        assert state.consecutive_failures == 3
        assert event3.to_phase == EnumDesignToPlanPhase.FAILED

    async def test_dry_run_propagated(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag propagates through state."""
        handler = HandlerDesignToPlan()
        command = _make_command(dry_run=True)

        state, _events, _completed = handler.run_full_pipeline(command)

        assert state.dry_run is True
        assert state.current_phase == EnumDesignToPlanPhase.DONE

    async def test_topic_propagated(self, event_bus: EventBusInmemory) -> None:
        """Topic string propagates from command to state."""
        handler = HandlerDesignToPlan()
        command = _make_command(topic="Build a CLI tool")

        state, _events, _completed = handler.run_full_pipeline(command)

        assert state.topic == "Build a CLI tool"

    async def test_plan_path_set_during_advance(
        self, event_bus: EventBusInmemory
    ) -> None:
        """plan_path can be set during advance."""
        handler = HandlerDesignToPlan()
        command = _make_command()
        state = handler.start(command)

        state, _ = handler.advance(state, phase_success=True)
        state, _ = handler.advance(
            state,
            phase_success=True,
            plan_path="docs/plans/2026-04-05-new-feature.md",
        )

        assert state.plan_path == "docs/plans/2026-04-05-new-feature.md"

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerDesignToPlan()
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelDesignToPlanCommand(
                correlation_id=payload["correlation_id"],
                topic=payload.get("topic", "test"),
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
            CMD_TOPIC, on_message=on_command, group_id="test-design-to-plan"
        )

        cmd_payload = json.dumps(
            {"correlation_id": str(uuid4()), "topic": "test topic"}
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["final_phase"] == "done"

        history = await event_bus.get_event_history(topic=COMPLETED_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_cannot_advance_from_terminal(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Advancing from DONE raises ValueError."""
        handler = HandlerDesignToPlan()
        command = _make_command()

        state, _, _ = handler.run_full_pipeline(command)
        assert state.current_phase == EnumDesignToPlanPhase.DONE

        with pytest.raises(ValueError, match="terminal phase"):
            handler.advance(state, phase_success=True)

    async def test_phase_event_serialization(self, event_bus: EventBusInmemory) -> None:
        """Phase events serialize to valid JSON bytes."""
        handler = HandlerDesignToPlan()
        command = _make_command()
        state = handler.start(command)
        state, event = handler.advance(state, phase_success=True)

        serialized = handler.serialize_event(event)
        deserialized = json.loads(serialized)

        assert deserialized["from_phase"] == "idle"
        assert deserialized["to_phase"] == "brainstorm"
        assert deserialized["success"] is True

    async def test_failure_resets_on_success(self, event_bus: EventBusInmemory) -> None:
        """A success after failures resets consecutive_failures to 0."""
        handler = HandlerDesignToPlan()
        command = _make_command()
        state = handler.start(command)

        state, _ = handler.advance(state, phase_success=True)
        state, _ = handler.advance(state, phase_success=False, error_message="fail 1")
        assert state.consecutive_failures == 1
        state, _ = handler.advance(state, phase_success=True)
        assert state.consecutive_failures == 0
        assert state.current_phase == EnumDesignToPlanPhase.STRUCTURE

    async def test_review_rounds_accumulate(self, event_bus: EventBusInmemory) -> None:
        """Review round counts accumulate across phase transitions."""
        handler = HandlerDesignToPlan()
        command = _make_command()
        state = handler.start(command)

        state, _ = handler.advance(state, phase_success=True, review_rounds=0)
        state, _ = handler.advance(state, phase_success=True, review_rounds=2)
        state, _ = handler.advance(state, phase_success=True, review_rounds=1)

        assert state.review_rounds == 3
