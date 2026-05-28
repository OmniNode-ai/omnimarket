"""Golden chain tests for node_design_to_plan.

Verifies the FSM state machine: start command -> phase transitions -> completion,
circuit breaker, dry_run, plan_path propagation, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.events.design_to_plan import ModelPlanToTicketsStartCommand
from omnimarket.nodes.node_design_to_plan.handlers.handler_design_to_plan import (
    HandlerDesignToPlan,
)
from omnimarket.nodes.node_design_to_plan.handlers.handler_design_to_plan_phase3_launch import (
    HandlerDesignToPlanPhase3Launch,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_command import (
    ModelDesignToPlanCommand,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_phase3_launch import (
    ModelDesignToPlanPhase3LaunchResult,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_state import (
    EnumDesignToPlanPhase,
    ModelDesignToPlanState,
)

CMD_TOPIC = "onex.cmd.omnimarket.design-to-plan-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.design-to-plan-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.design-to-plan-completed.v1"


def _make_command(
    topic: str = "Build a new dashboard",
    dry_run: bool = False,
    no_launch: bool = False,
    plan_only: bool = False,
) -> ModelDesignToPlanCommand:
    return ModelDesignToPlanCommand(
        correlation_id=uuid4(),
        topic=topic,
        no_launch=no_launch,
        dry_run=dry_run,
        plan_only=plan_only,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestDesignToPlanGoldenChain:
    """Golden chain: start command -> phase transitions -> completion."""

    async def test_full_cycle_all_phases_succeed(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All phases (including Phase 3 LAUNCH stub) succeed -> DONE."""
        handler = HandlerDesignToPlan()
        command = _make_command()

        state, events, completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumDesignToPlanPhase.DONE
        assert state.consecutive_failures == 0
        assert state.error_message is None
        assert completed.final_phase == EnumDesignToPlanPhase.DONE
        # 6 transitions: IDLE->BRAINSTORM, BRAINSTORM->STRUCTURE,
        # STRUCTURE->REVIEW, REVIEW->FINALIZE, FINALIZE->LAUNCH, LAUNCH->DONE
        # Phase 3 (LAUNCH) is a stub — FSM advances through it when phase_success=True
        assert len(events) == 6
        assert all(e.success for e in events)
        assert events[0].from_phase == EnumDesignToPlanPhase.IDLE
        assert events[0].to_phase == EnumDesignToPlanPhase.BRAINSTORM
        assert events[-2].from_phase == EnumDesignToPlanPhase.FINALIZE
        assert events[-2].to_phase == EnumDesignToPlanPhase.LAUNCH
        assert events[-1].from_phase == EnumDesignToPlanPhase.LAUNCH
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

    async def test_phase3_launch_is_in_sequence(
        self, event_bus: EventBusInmemory
    ) -> None:
        """LAUNCH phase sits between FINALIZE and DONE in the FSM sequence (OMN-12228)."""
        from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_state import (
            next_phase,
        )

        assert (
            next_phase(EnumDesignToPlanPhase.FINALIZE) == EnumDesignToPlanPhase.LAUNCH
        )
        assert next_phase(EnumDesignToPlanPhase.LAUNCH) == EnumDesignToPlanPhase.DONE

    async def test_phase3_launch_builds_plan_to_tickets_native_dispatch(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Phase 3 builds a typed Onex-native plan_to_tickets command."""
        handler = HandlerDesignToPlanPhase3Launch()
        command = ModelDesignToPlanCommand(
            correlation_id=uuid4(),
            topic="Build deterministic Phase 3 routing",
            plan_only=True,
            requested_at=datetime.now(tz=UTC),
        )
        state = ModelDesignToPlanState(
            correlation_id=command.correlation_id,
            current_phase=EnumDesignToPlanPhase.LAUNCH,
            plan_path="docs/plans/omn-12359.md",
        )

        result = handler.handle(command, state)

        assert isinstance(result, ModelDesignToPlanPhase3LaunchResult)
        assert result.status == "planned"
        assert result.plan_only is True
        assert len(result.dispatches) == 1

        dispatch = result.dispatches[0]
        assert dispatch.route_id == "plan_to_tickets"
        assert dispatch.target_node == "node_plan_to_tickets"
        assert dispatch.command_topic == "onex.cmd.omnimarket.plan-to-tickets-start.v1"
        assert (
            dispatch.command_model
            == "omnimarket.events.design_to_plan.ModelPlanToTicketsStartCommand"
        )
        assert isinstance(dispatch.command, ModelPlanToTicketsStartCommand)
        assert dispatch.command.correlation_id == str(command.correlation_id)
        assert dispatch.command.plan_path == "docs/plans/omn-12359.md"
        assert dispatch.command.epic_title == "Build deterministic Phase 3 routing"
        assert dispatch.command.dry_run is True
        assert dispatch.command.repo == "omnimarket"

    async def test_phase3_launch_dry_run_marks_downstream_command_dry_run(
        self, event_bus: EventBusInmemory
    ) -> None:
        """dry_run is contract-declared and preserves a non-mutating native command."""
        command = _make_command(dry_run=True)
        state = ModelDesignToPlanState(
            correlation_id=command.correlation_id,
            current_phase=EnumDesignToPlanPhase.LAUNCH,
            plan_path="/tmp/omn-12359-plan.md",
        )

        result = HandlerDesignToPlanPhase3Launch().handle(command, state)

        assert result.status == "planned"
        assert result.dry_run is True
        assert result.dispatches[0].command.dry_run is True

    async def test_phase3_launch_requires_finalized_plan_path(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Phase 3 cannot report success without a finalized plan path."""
        command = _make_command()
        state = ModelDesignToPlanState(
            correlation_id=command.correlation_id,
            current_phase=EnumDesignToPlanPhase.LAUNCH,
        )

        with pytest.raises(ValueError, match="plan_path"):
            HandlerDesignToPlanPhase3Launch().handle(command, state)

    async def test_phase3_launch_no_launch_skips_without_plan_path(
        self, event_bus: EventBusInmemory
    ) -> None:
        """no_launch never builds a downstream dispatch, even without plan_path."""
        command = _make_command(no_launch=True)
        state = ModelDesignToPlanState(
            correlation_id=command.correlation_id,
            current_phase=EnumDesignToPlanPhase.LAUNCH,
            no_launch=True,
        )

        result = HandlerDesignToPlanPhase3Launch().handle(command, state)

        assert result.status == "skipped"
        assert result.plan_path is None
        assert result.dispatches == ()

    async def test_phase3_launch_requires_launch_phase(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Phase 3 routing only runs from the LAUNCH FSM phase."""
        command = _make_command()
        state = ModelDesignToPlanState(
            correlation_id=command.correlation_id,
            current_phase=EnumDesignToPlanPhase.FINALIZE,
            plan_path="/tmp/omn-12359-plan.md",
        )

        with pytest.raises(ValueError, match="current_phase='launch'"):
            HandlerDesignToPlanPhase3Launch().handle(command, state)

    async def test_phase3_launch_publish_payload_matches_downstream_model(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Returned dispatches are directly publishable to the native command topic."""
        command = _make_command(plan_only=True)
        state = ModelDesignToPlanState(
            correlation_id=command.correlation_id,
            current_phase=EnumDesignToPlanPhase.LAUNCH,
            plan_path="/tmp/omn-12359-plan.md",
        )
        result = HandlerDesignToPlanPhase3Launch().handle(command, state)
        dispatch = result.dispatches[0]

        await event_bus.start()
        await event_bus.publish(
            dispatch.command_topic,
            key=None,
            value=dispatch.command.model_dump_json().encode(),
        )

        history = await event_bus.get_event_history(topic=dispatch.command_topic)
        assert len(history) == 1
        payload = json.loads(history[0].value)
        parsed = ModelPlanToTicketsStartCommand(**payload)
        assert parsed.plan_path == "/tmp/omn-12359-plan.md"
        assert parsed.dry_run is True

        await event_bus.close()

    async def test_phase3_contract_declares_implemented_native_route(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Contract route must match the downstream node command model/topic."""
        root = Path(__file__).resolve().parents[1]
        design_contract = yaml.safe_load(
            (root / "src/omnimarket/nodes/node_design_to_plan/contract.yaml").read_text(
                encoding="utf-8"
            )
        )
        downstream_contract = yaml.safe_load(
            (
                root / "src/omnimarket/nodes/node_plan_to_tickets/contract.yaml"
            ).read_text(encoding="utf-8")
        )

        route = design_contract["metadata"]["phase3_launch"]["routes"][0]
        assert design_contract["metadata"]["phase3_handler_status"] == "implemented"
        assert "dry_run" in design_contract["inputs"]
        assert "plan_only" in design_contract["inputs"]
        assert route["target_node"] == "node_plan_to_tickets"
        assert (
            route["command_topic"]
            in downstream_contract["event_bus"]["subscribe_topics"]
        )
        assert route["command_topic"] in design_contract["event_bus"]["publish_topics"]
        assert route["command_model"] == downstream_contract["handler"]["input_model"]
