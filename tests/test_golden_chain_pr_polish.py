"""Golden chain tests for node_pr_polish.

Verifies the FSM state machine: start command -> phase transitions -> completion,
skip_conflicts, circuit breaker, dry_run, metrics, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_pr_polish.handlers.handler_pr_polish import (
    HandlerPrPolish,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_start_command import (
    ModelPrPolishStartCommand,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_state import (
    EnumPrPolishPhase,
)

CMD_TOPIC = "onex.cmd.omnimarket.pr-polish-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.pr-polish-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.pr-polish-completed.v1"


def _make_command(
    pr_number: int | None = 42,
    skip_conflicts: bool = False,
    dry_run: bool = False,
) -> ModelPrPolishStartCommand:
    return ModelPrPolishStartCommand(
        correlation_id=uuid4(),
        pr_number=pr_number,
        skip_conflicts=skip_conflicts,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestPrPolishGoldenChain:
    """Golden chain: start command -> phase transitions -> completion."""

    async def test_full_cycle_all_phases_succeed(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All 5 phases succeed -> DONE."""
        handler = HandlerPrPolish()
        command = _make_command()

        state, events, completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumPrPolishPhase.DONE
        assert state.consecutive_failures == 0
        assert completed.final_phase == EnumPrPolishPhase.DONE

        # 5 transitions: INIT->RESOLVE_CONFLICTS, RESOLVE_CONFLICTS->FIX_CI,
        # FIX_CI->ADDRESS_COMMENTS, ADDRESS_COMMENTS->LOCAL_REVIEW, LOCAL_REVIEW->DONE
        assert len(events) == 5
        assert all(e.success for e in events)

    async def test_skip_conflicts(self, event_bus: EventBusInmemory) -> None:
        """skip_conflicts=True skips RESOLVE_CONFLICTS phase."""
        handler = HandlerPrPolish()
        command = _make_command(skip_conflicts=True)

        state, events, _completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumPrPolishPhase.DONE
        # 4 transitions (no RESOLVE_CONFLICTS)
        assert len(events) == 4
        phase_names = [e.to_phase for e in events]
        assert EnumPrPolishPhase.RESOLVE_CONFLICTS not in phase_names

    async def test_circuit_breaker_after_3_failures(
        self, event_bus: EventBusInmemory
    ) -> None:
        """3 consecutive failures -> FAILED."""
        handler = HandlerPrPolish()
        command = _make_command()
        state = handler.start(command)

        state, _ = handler.advance(state, phase_success=True)
        assert state.current_phase == EnumPrPolishPhase.RESOLVE_CONFLICTS

        state, _ = handler.advance(state, phase_success=False, error_message="fail 1")
        state, _ = handler.advance(state, phase_success=False, error_message="fail 2")
        state, _ = handler.advance(state, phase_success=False, error_message="fail 3")
        assert state.current_phase == EnumPrPolishPhase.FAILED

    async def test_circuit_breaker_via_run_full_pipeline(
        self, event_bus: EventBusInmemory
    ) -> None:
        """run_full_pipeline with a failing phase breaks on first failure."""
        handler = HandlerPrPolish()
        command = _make_command()

        state, _events, completed = handler.run_full_pipeline(
            command,
            phase_results={EnumPrPolishPhase.FIX_CI: False},
        )

        assert completed.final_phase == EnumPrPolishPhase.RESOLVE_CONFLICTS
        assert state.consecutive_failures == 1

    async def test_dry_run_propagated(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag propagates through state."""
        handler = HandlerPrPolish()
        command = _make_command(dry_run=True)

        state, _events, _completed = handler.run_full_pipeline(command)

        assert state.dry_run is True
        assert state.current_phase == EnumPrPolishPhase.DONE

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerPrPolish()
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelPrPolishStartCommand(
                correlation_id=payload["correlation_id"],
                pr_number=payload.get("pr_number"),
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
            CMD_TOPIC, on_message=on_command, group_id="test-pr-polish"
        )

        cmd_payload = json.dumps(
            {"correlation_id": str(uuid4()), "pr_number": 42}
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["final_phase"] == "done"

        await event_bus.close()

    async def test_cannot_advance_from_terminal(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Advancing from DONE raises ValueError."""
        handler = HandlerPrPolish()
        command = _make_command()
        state, _, _ = handler.run_full_pipeline(command)

        with pytest.raises(ValueError, match="terminal phase"):
            handler.advance(state, phase_success=True)

    async def test_metrics_accumulate(self, event_bus: EventBusInmemory) -> None:
        """Metrics accumulate across phases."""
        handler = HandlerPrPolish()
        command = _make_command()
        state = handler.start(command)

        state, _ = handler.advance(state, phase_success=True, conflicts_resolved=2)
        assert state.conflicts_resolved == 2
        state, _ = handler.advance(state, phase_success=True, ci_fixes_applied=3)
        assert state.ci_fixes_applied == 3
        state, _ = handler.advance(state, phase_success=True, comments_addressed=5)
        assert state.comments_addressed == 5

    async def test_serialization(self, event_bus: EventBusInmemory) -> None:
        """Events serialize to valid JSON."""
        handler = HandlerPrPolish()
        command = _make_command()
        _, _, completed = handler.run_full_pipeline(command)

        serialized = handler.serialize_completed(completed)
        deserialized = json.loads(serialized)
        assert deserialized["final_phase"] == "done"
        assert deserialized["pr_number"] == 42
