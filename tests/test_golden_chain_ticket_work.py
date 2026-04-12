"""Golden chain tests for node_ticket_work.

Verifies the FSM state machine: start command -> phase transitions -> completion,
circuit breaker, autonomous mode, PR URL tracking, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_ticket_work.handlers.handler_ticket_work import (
    HandlerTicketWork,
)
from omnimarket.nodes.node_ticket_work.models.model_ticket_work_command import (
    ModelTicketWorkCommand,
)
from omnimarket.nodes.node_ticket_work.models.model_ticket_work_state import (
    EnumTicketWorkPhase,
)

CMD_TOPIC = "onex.cmd.omnimarket.ticket-work-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.ticket-work-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.ticket-work-completed.v1"


def _make_command(
    ticket_id: str = "OMN-1234",
    autonomous: bool = False,
    dry_run: bool = False,
) -> ModelTicketWorkCommand:
    return ModelTicketWorkCommand(
        correlation_id=uuid4(),
        ticket_id=ticket_id,
        autonomous=autonomous,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestTicketWorkGoldenChain:
    """Golden chain: start command -> phase transitions -> completion."""

    async def test_full_cycle_all_phases_succeed(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All 7 phases succeed -> DONE."""
        handler = HandlerTicketWork()
        command = _make_command()

        state, events, completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumTicketWorkPhase.DONE
        assert state.ticket_id == "OMN-1234"
        assert completed.final_phase == EnumTicketWorkPhase.DONE
        # 7 transitions: IDLE->INTAKE->RESEARCH->QUESTIONS->SPEC->IMPLEMENT->REVIEW->DONE
        assert len(events) == 7
        assert all(e.success for e in events)

    async def test_circuit_breaker_after_3_failures(
        self, event_bus: EventBusInmemory
    ) -> None:
        """3 consecutive failures -> FAILED."""
        handler = HandlerTicketWork()
        command = _make_command()
        state = handler.start(command)

        state, _ = handler.advance(state, phase_success=True)
        assert state.current_phase == EnumTicketWorkPhase.INTAKE

        state, _ = handler.advance(state, phase_success=False, error_message="fail 1")
        state, _ = handler.advance(state, phase_success=False, error_message="fail 2")
        state, event3 = handler.advance(
            state, phase_success=False, error_message="fail 3"
        )
        assert state.current_phase == EnumTicketWorkPhase.FAILED
        assert event3.to_phase == EnumTicketWorkPhase.FAILED

    async def test_autonomous_flag_propagated(
        self, event_bus: EventBusInmemory
    ) -> None:
        """autonomous flag propagates through state."""
        handler = HandlerTicketWork()
        command = _make_command(autonomous=True)

        state, _events, _completed = handler.run_full_pipeline(command)

        assert state.autonomous is True
        assert state.current_phase == EnumTicketWorkPhase.DONE

    async def test_pr_url_set_during_advance(self, event_bus: EventBusInmemory) -> None:
        """PR URL can be set during the review phase advance."""
        handler = HandlerTicketWork()
        command = _make_command()
        state = handler.start(command)

        # Advance through phases
        for _ in range(5):
            state, _ = handler.advance(state, phase_success=True)

        assert state.current_phase == EnumTicketWorkPhase.IMPLEMENT

        state, _ = handler.advance(
            state,
            phase_success=True,
            pr_url="https://github.com/org/repo/pull/42",
            commits=["abc1234"],
        )

        assert state.pr_url == "https://github.com/org/repo/pull/42"
        assert state.commits == ["abc1234"]

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerTicketWork()
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelTicketWorkCommand(
                correlation_id=payload["correlation_id"],
                ticket_id=payload.get("ticket_id", "OMN-0001"),
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
            CMD_TOPIC, on_message=on_command, group_id="test-ticket-work"
        )

        cmd_payload = json.dumps(
            {"correlation_id": str(uuid4()), "ticket_id": "OMN-5678"}
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["final_phase"] == "done"
        assert completed_events[0]["ticket_id"] == "OMN-5678"

        await event_bus.close()

    async def test_cannot_advance_from_terminal(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Advancing from DONE raises ValueError."""
        handler = HandlerTicketWork()
        command = _make_command()
        state, _, _ = handler.run_full_pipeline(command)

        with pytest.raises(ValueError, match="terminal phase"):
            handler.advance(state, phase_success=True)

    async def test_phase_failure_stops_pipeline(
        self, event_bus: EventBusInmemory
    ) -> None:
        """run_full_pipeline stops on first failure."""
        handler = HandlerTicketWork()
        command = _make_command()

        state, _events, completed = handler.run_full_pipeline(
            command,
            phase_results={EnumTicketWorkPhase.SPEC: False},
        )

        assert completed.final_phase == EnumTicketWorkPhase.QUESTIONS
        assert state.consecutive_failures == 1
