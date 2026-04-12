"""Golden chain tests for node_release.

Verifies the FSM state machine: start command -> phase transitions -> completion,
circuit breaker, repo metrics, dry_run, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_release.handlers.handler_release import (
    HandlerRelease,
)
from omnimarket.nodes.node_release.models.model_release_command import (
    ModelReleaseCommand,
)
from omnimarket.nodes.node_release.models.model_release_state import (
    EnumReleasePhase,
)

CMD_TOPIC = "onex.cmd.omnimarket.release-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.release-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.release-completed.v1"


def _make_command(
    repos: list[str] | None = None,
    bump: str | None = None,
    dry_run: bool = False,
) -> ModelReleaseCommand:
    return ModelReleaseCommand(
        correlation_id=uuid4(),
        repos=repos or ["omnibase_core", "omnibase_infra"],
        bump=bump,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestReleaseGoldenChain:
    """Golden chain: start command -> phase transitions -> completion."""

    async def test_full_cycle_all_phases_succeed(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All 6 phases succeed -> DONE."""
        handler = HandlerRelease()
        command = _make_command()

        state, events, completed = handler.run_full_pipeline(command)

        assert state.current_phase == EnumReleasePhase.DONE
        assert completed.final_phase == EnumReleasePhase.DONE
        # 7 transitions: IDLE->BUMP..->PIN..->CREATE..->MERGE->TAG->PUBLISH->DONE
        assert len(events) == 7
        assert all(e.success for e in events)
        assert events[0].from_phase == EnumReleasePhase.IDLE
        assert events[0].to_phase == EnumReleasePhase.BUMP_VERSIONS
        assert events[-1].to_phase == EnumReleasePhase.DONE

    async def test_circuit_breaker_after_3_failures(
        self, event_bus: EventBusInmemory
    ) -> None:
        """3 consecutive failures -> FAILED."""
        handler = HandlerRelease()
        command = _make_command()
        state = handler.start(command)

        state, _ = handler.advance(state, phase_success=True)
        state, _ = handler.advance(state, phase_success=False, error_message="fail 1")
        state, _ = handler.advance(state, phase_success=False, error_message="fail 2")
        state, _ = handler.advance(state, phase_success=False, error_message="fail 3")

        assert state.current_phase == EnumReleasePhase.FAILED

    async def test_repos_propagated(self, event_bus: EventBusInmemory) -> None:
        """Repos list propagates from command to state."""
        handler = HandlerRelease()
        command = _make_command(repos=["omnibase_spi"])

        state, _events, _completed = handler.run_full_pipeline(command)

        assert state.repos == ["omnibase_spi"]

    async def test_bump_override_propagated(self, event_bus: EventBusInmemory) -> None:
        """Bump override propagates from command to state."""
        handler = HandlerRelease()
        command = _make_command(bump="minor")

        state, _events, _completed = handler.run_full_pipeline(command)

        assert state.bump == "minor"

    async def test_repo_metrics_accumulate(self, event_bus: EventBusInmemory) -> None:
        """Repo success/fail/skip metrics accumulate across phases."""
        handler = HandlerRelease()
        command = _make_command()
        state = handler.start(command)

        state, _ = handler.advance(
            state, phase_success=True, repos_succeeded=3, repos_failed=1
        )
        state, _ = handler.advance(state, phase_success=True, repos_skipped=2)

        assert state.repos_succeeded == 3
        assert state.repos_failed == 1
        assert state.repos_skipped == 2

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerRelease()
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelReleaseCommand(
                correlation_id=payload["correlation_id"],
                repos=payload.get("repos", []),
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
            CMD_TOPIC, on_message=on_command, group_id="test-release"
        )

        cmd_payload = json.dumps(
            {"correlation_id": str(uuid4()), "repos": ["omnibase_core"]}
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["final_phase"] == "done"

        await event_bus.close()

    async def test_cannot_advance_from_terminal(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Advancing from DONE raises ValueError."""
        handler = HandlerRelease()
        command = _make_command()
        state, _, _ = handler.run_full_pipeline(command)

        with pytest.raises(ValueError, match="terminal phase"):
            handler.advance(state, phase_success=True)

    async def test_dry_run_propagated(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag propagates through state."""
        handler = HandlerRelease()
        command = _make_command(dry_run=True)

        state, _events, _completed = handler.run_full_pipeline(command)
        assert state.dry_run is True

    async def test_phase_failure_stops_pipeline(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Pipeline stops on first failure in run_full_pipeline."""
        handler = HandlerRelease()
        command = _make_command()

        state, _events, completed = handler.run_full_pipeline(
            command,
            phase_results={EnumReleasePhase.MERGE: False},
        )

        assert completed.final_phase == EnumReleasePhase.CREATE_PRS
        assert state.consecutive_failures == 1
