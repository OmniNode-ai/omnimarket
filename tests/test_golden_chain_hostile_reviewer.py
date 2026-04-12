"""Golden chain tests for node_hostile_reviewer.

Verifies the FSM state machine: start command -> phase transitions -> completion,
circuit breaker, dry_run, findings tracking, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_hostile_reviewer import (
    HandlerHostileReviewer,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_state import (
    EnumHostileReviewerPhase,
)

CMD_TOPIC = "onex.cmd.omnimarket.hostile-reviewer-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.hostile-reviewer-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.hostile-reviewer-completed.v1"


def _make_command(
    dry_run: bool = False,
    pr_number: int | None = 42,
) -> ModelHostileReviewerStartCommand:
    return ModelHostileReviewerStartCommand(
        correlation_id=uuid4(),
        pr_number=pr_number,
        repo="OmniNode-ai/test",
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestHostileReviewerGoldenChain:
    """Golden chain: start command -> phase transitions -> completion."""

    async def test_full_cycle_all_phases_succeed(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All 5 phases succeed -> DONE."""
        handler = HandlerHostileReviewer()
        command = _make_command()

        state, events, completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumHostileReviewerPhase.DONE
        assert state.consecutive_failures == 0
        assert state.error_message is None
        assert completed.final_phase == EnumHostileReviewerPhase.DONE

        # 5 transitions: INIT->DISPATCH_REVIEWS, DISPATCH_REVIEWS->AGGREGATE,
        # AGGREGATE->CONVERGENCE_CHECK, CONVERGENCE_CHECK->REPORT, REPORT->DONE
        assert len(events) == 5
        assert all(e.success for e in events)
        assert events[0].from_phase == EnumHostileReviewerPhase.INIT
        assert events[0].to_phase == EnumHostileReviewerPhase.DISPATCH_REVIEWS
        assert events[-1].from_phase == EnumHostileReviewerPhase.REPORT
        assert events[-1].to_phase == EnumHostileReviewerPhase.DONE

    async def test_circuit_breaker_after_3_failures(
        self, event_bus: EventBusInmemory
    ) -> None:
        """3 consecutive failures -> FAILED."""
        handler = HandlerHostileReviewer()
        command = _make_command()
        state = handler.start(command)

        # INIT -> DISPATCH_REVIEWS (success)
        state, _ = handler.advance(state, phase_success=True)
        assert state.current_phase == EnumHostileReviewerPhase.DISPATCH_REVIEWS

        # Fail 3 times
        state, _ = handler.advance(state, phase_success=False, error_message="fail 1")
        assert state.consecutive_failures == 1
        state, _ = handler.advance(state, phase_success=False, error_message="fail 2")
        assert state.consecutive_failures == 2
        state, event3 = handler.advance(
            state, phase_success=False, error_message="fail 3"
        )
        assert state.current_phase == EnumHostileReviewerPhase.FAILED
        assert event3.success is False

    async def test_circuit_breaker_via_run_full_pipeline(
        self, event_bus: EventBusInmemory
    ) -> None:
        """run_full_pipeline with a failing phase breaks on first failure."""
        handler = HandlerHostileReviewer()
        command = _make_command()

        state, _events, completed = handler.run_full_pipeline(
            command,
            phase_results={EnumHostileReviewerPhase.AGGREGATE: False},
        )

        assert completed.final_phase == EnumHostileReviewerPhase.DISPATCH_REVIEWS
        assert state.consecutive_failures == 1

    async def test_dry_run_propagated(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag propagates through state."""
        handler = HandlerHostileReviewer()
        command = _make_command(dry_run=True)

        state, _events, _completed = handler.run_full_pipeline(command)

        assert state.dry_run is True
        assert state.current_phase == EnumHostileReviewerPhase.DONE

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerHostileReviewer()
        completed_events: list[dict[str, object]] = []
        phase_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelHostileReviewerStartCommand(
                correlation_id=payload["correlation_id"],
                pr_number=payload.get("pr_number"),
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
            CMD_TOPIC, on_message=on_command, group_id="test-hostile-reviewer"
        )

        cmd_payload = json.dumps(
            {"correlation_id": str(uuid4()), "pr_number": 42}
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["final_phase"] == "done"
        assert len(phase_events) == 5

        await event_bus.close()

    async def test_failure_resets_on_success(self, event_bus: EventBusInmemory) -> None:
        """A success after failures resets consecutive_failures."""
        handler = HandlerHostileReviewer()
        command = _make_command()
        state = handler.start(command)

        state, _ = handler.advance(state, phase_success=True)
        state, _ = handler.advance(state, phase_success=False, error_message="fail")
        assert state.consecutive_failures == 1
        state, _ = handler.advance(state, phase_success=True)
        assert state.consecutive_failures == 0

    async def test_cannot_advance_from_terminal(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Advancing from DONE raises ValueError."""
        handler = HandlerHostileReviewer()
        command = _make_command()
        state, _, _ = handler.run_full_pipeline(command)

        with pytest.raises(ValueError, match="terminal phase"):
            handler.advance(state, phase_success=True)

    async def test_findings_accumulate(self, event_bus: EventBusInmemory) -> None:
        """Findings accumulate across phases."""
        handler = HandlerHostileReviewer()
        command = _make_command()
        state = handler.start(command)

        state, _ = handler.advance(state, phase_success=True, findings=3)
        assert state.total_findings == 3
        state, _ = handler.advance(state, phase_success=True, findings=2)
        assert state.total_findings == 5

    async def test_serialization(self, event_bus: EventBusInmemory) -> None:
        """Events serialize to valid JSON bytes."""
        handler = HandlerHostileReviewer()
        command = _make_command()
        state = handler.start(command)
        state, event = handler.advance(state, phase_success=True)

        serialized = handler.serialize_event(event)
        deserialized = json.loads(serialized)
        assert deserialized["from_phase"] == "init"
        assert deserialized["to_phase"] == "dispatch_reviews"

        _, _, completed = handler.run_full_pipeline(command)
        serialized_c = handler.serialize_completed(completed)
        deserialized_c = json.loads(serialized_c)
        assert deserialized_c["final_phase"] == "done"
