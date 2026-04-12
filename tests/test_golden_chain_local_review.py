"""Golden chain tests for node_local_review.

Verifies the FSM state machine: start command -> review/fix/commit/check loop,
convergence, circuit breaker, dry_run, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_local_review.handlers.handler_local_review import (
    HandlerLocalReview,
)
from omnimarket.nodes.node_local_review.models.model_local_review_start_command import (
    ModelLocalReviewStartCommand,
)
from omnimarket.nodes.node_local_review.models.model_local_review_state import (
    EnumLocalReviewPhase,
)

CMD_TOPIC = "onex.cmd.omnimarket.local-review-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.local-review-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.local-review-completed.v1"


def _make_command(
    dry_run: bool = False,
    max_iterations: int = 10,
    required_clean_runs: int = 2,
) -> ModelLocalReviewStartCommand:
    return ModelLocalReviewStartCommand(
        correlation_id=uuid4(),
        max_iterations=max_iterations,
        required_clean_runs=required_clean_runs,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestLocalReviewGoldenChain:
    """Golden chain: start command -> review loop -> completion."""

    async def test_single_clean_iteration(self, event_bus: EventBusInmemory) -> None:
        """Single clean iteration -> DONE (check_clean_results=[True])."""
        handler = HandlerLocalReview()
        command = _make_command()

        state, events, completed = handler.run_full_pipeline(
            command, check_clean_results=[True]
        )

        assert state.current_phase == EnumLocalReviewPhase.DONE
        assert completed.final_phase == EnumLocalReviewPhase.DONE
        # INIT->REVIEW, REVIEW->FIX, FIX->COMMIT, COMMIT->CHECK_CLEAN, CHECK_CLEAN->DONE
        assert len(events) == 5

    async def test_loop_on_dirty_then_clean(self, event_bus: EventBusInmemory) -> None:
        """First check dirty, second clean -> loops back then completes."""
        handler = HandlerLocalReview()
        command = _make_command()

        state, events, _completed = handler.run_full_pipeline(
            command, check_clean_results=[False, True]
        )

        assert state.current_phase == EnumLocalReviewPhase.DONE
        # First pass: INIT->REVIEW, REVIEW->FIX, FIX->COMMIT, COMMIT->CHECK_CLEAN,
        #   CHECK_CLEAN->REVIEW (loop)
        # Second pass: REVIEW->FIX, FIX->COMMIT, COMMIT->CHECK_CLEAN, CHECK_CLEAN->DONE
        assert len(events) == 9
        assert state.iteration_count == 2  # entered REVIEW twice (initial + loop back)

    async def test_circuit_breaker_after_3_failures(
        self, event_bus: EventBusInmemory
    ) -> None:
        """3 consecutive failures -> FAILED."""
        handler = HandlerLocalReview()
        command = _make_command()
        state = handler.start(command)

        # INIT -> REVIEW (success)
        state, _ = handler.advance(state, phase_success=True)
        assert state.current_phase == EnumLocalReviewPhase.REVIEW

        # Fail 3 times
        state, _ = handler.advance(state, phase_success=False, error_message="fail 1")
        assert state.consecutive_failures == 1
        state, _ = handler.advance(state, phase_success=False, error_message="fail 2")
        assert state.consecutive_failures == 2
        state, event3 = handler.advance(
            state, phase_success=False, error_message="fail 3"
        )
        assert state.current_phase == EnumLocalReviewPhase.FAILED
        assert event3.success is False

    async def test_max_iterations_stops(self, event_bus: EventBusInmemory) -> None:
        """Max iterations reached -> DONE with error message."""
        handler = HandlerLocalReview()
        command = _make_command(max_iterations=2)

        state, _events, _completed = handler.run_full_pipeline(
            command, check_clean_results=[False, False, False]
        )

        assert state.current_phase == EnumLocalReviewPhase.DONE
        assert state.error_message == "Max iterations reached"

    async def test_dry_run_propagated(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag propagates through state."""
        handler = HandlerLocalReview()
        command = _make_command(dry_run=True)

        state, _events, _completed = handler.run_full_pipeline(command)

        assert state.dry_run is True
        assert state.current_phase == EnumLocalReviewPhase.DONE

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerLocalReview()
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelLocalReviewStartCommand(
                correlation_id=payload["correlation_id"],
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
            CMD_TOPIC, on_message=on_command, group_id="test-local-review"
        )

        cmd_payload = json.dumps({"correlation_id": str(uuid4())}).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["final_phase"] == "done"

        await event_bus.close()

    async def test_cannot_advance_from_terminal(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Advancing from DONE raises ValueError."""
        handler = HandlerLocalReview()
        command = _make_command()
        state, _, _ = handler.run_full_pipeline(command)

        with pytest.raises(ValueError, match="terminal phase"):
            handler.advance(state, phase_success=True)

    async def test_issues_accumulate(self, event_bus: EventBusInmemory) -> None:
        """Issue counts accumulate across phases."""
        handler = HandlerLocalReview()
        command = _make_command()
        state = handler.start(command)

        state, _ = handler.advance(state, phase_success=True, issues_found=5)
        assert state.issues_found == 5
        state, _ = handler.advance(state, phase_success=True, issues_fixed=3)
        assert state.issues_fixed == 3

    async def test_serialization(self, event_bus: EventBusInmemory) -> None:
        """Events serialize to valid JSON bytes."""
        handler = HandlerLocalReview()
        command = _make_command()
        state = handler.start(command)
        state, event = handler.advance(state, phase_success=True)

        serialized = handler.serialize_event(event)
        deserialized = json.loads(serialized)
        assert deserialized["from_phase"] == "init"
        assert deserialized["to_phase"] == "review"
