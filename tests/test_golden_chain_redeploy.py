"""Golden chain tests for node_redeploy.

Verifies:
  - TestRedeployGoldenChain: FSM state machine (HandlerRedeploy)
  - TestRedeployKafkaGoldenChain: Kafka publish-monitor pattern (HandlerRedeployKafka)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_redeploy.handlers.handler_redeploy import HandlerRedeploy
from omnimarket.nodes.node_redeploy.handlers.handler_redeploy_kafka import (
    HandlerRedeployKafka,
)
from omnimarket.nodes.node_redeploy.models.model_deploy_agent_events import (
    EnumRedeployStatus,
    ModelDeployPhaseResults,
    ModelDeployRebuildCompleted,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    ModelRedeployCommand,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_state import (
    EnumRedeployPhase,
)

_EVT_TOPIC = "onex.evt.deploy.rebuild-completed.v1"
_CMD_TOPIC = "onex.cmd.deploy.rebuild-requested.v1"

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


# ---------------------------------------------------------------------------
# HandlerRedeployKafka golden chain tests
# ---------------------------------------------------------------------------


def _make_completed_event(
    correlation_id: str,
    status: str = "success",
    duration_seconds: float = 12.5,
    git_sha: str = "deadbeef",
    services_restarted: list[str] | None = None,
    errors: list[str] | None = None,
) -> ModelDeployRebuildCompleted:
    return ModelDeployRebuildCompleted(
        correlation_id=correlation_id,
        status=EnumRedeployStatus(status),
        duration_seconds=duration_seconds,
        git_sha=git_sha,
        services_restarted=services_restarted or ["omninode-runtime"],
        phase_results=ModelDeployPhaseResults(),
        errors=errors or [],
    )


@pytest.mark.unit
class TestRedeployKafkaGoldenChain:
    """Golden chain: HandlerRedeployKafka publish-monitor with EventBusInmemory."""

    async def test_full_success_cycle(self) -> None:
        """Publish command -> deploy agent responds with success -> result is success."""
        bus = EventBusInmemory(environment="test", group="redeploy-test")
        await bus.start()

        corr_id = str(uuid4())
        handler = HandlerRedeployKafka(event_bus=bus, timeout_s=5.0)

        # Simulate the deploy agent: subscribe to cmd topic, publish completion
        async def _fake_deploy_agent(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            completion = _make_completed_event(
                correlation_id=payload["correlation_id"],
                git_sha="abc123",
                services_restarted=["omninode-runtime", "omninode-runtime-shadow"],
            )
            await bus.publish(
                _EVT_TOPIC,
                key=payload["correlation_id"].encode(),
                value=json.dumps(completion.model_dump(mode="json")).encode(),
            )

        await bus.subscribe(
            _CMD_TOPIC, on_message=_fake_deploy_agent, group_id="fake-agent"
        )

        result = await handler.execute(
            scope="full",
            git_ref="origin/main",
            correlation_id=corr_id,
        )

        assert result.success is True
        assert result.status == EnumRedeployStatus.SUCCESS
        assert result.correlation_id == corr_id
        assert result.git_sha == "abc123"
        assert "omninode-runtime" in result.services_restarted
        assert result.timed_out is False

        await bus.close()

    async def test_deploy_agent_reports_failure(self) -> None:
        """Deploy agent returns failed status -> result is failure."""
        bus = EventBusInmemory(environment="test", group="redeploy-test")
        await bus.start()

        corr_id = str(uuid4())
        handler = HandlerRedeployKafka(event_bus=bus, timeout_s=5.0)

        async def _failed_agent(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            completion = _make_completed_event(
                correlation_id=payload["correlation_id"],
                status="failed",
                errors=["git pull failed: merge conflict"],
            )
            await bus.publish(
                _EVT_TOPIC,
                key=payload["correlation_id"].encode(),
                value=json.dumps(completion.model_dump(mode="json")).encode(),
            )

        await bus.subscribe(_CMD_TOPIC, on_message=_failed_agent, group_id="fake-agent")

        result = await handler.execute(scope="full", correlation_id=corr_id)

        assert result.success is False
        assert result.status == EnumRedeployStatus.FAILED
        assert "git pull failed" in result.errors[0]
        assert result.timed_out is False

        await bus.close()

    async def test_timeout_when_no_agent_responds(self) -> None:
        """No deploy agent responds -> timeout -> timed_out=True."""
        bus = EventBusInmemory(environment="test", group="redeploy-test")
        await bus.start()

        handler = HandlerRedeployKafka(event_bus=bus, timeout_s=0.1)

        result = await handler.execute(scope="full")

        assert result.success is False
        assert result.timed_out is True
        assert result.status == EnumRedeployStatus.FAILED
        assert "Timed out" in result.errors[0]

        await bus.close()

    async def test_correlation_id_filtering(self) -> None:
        """Only the matching correlation_id resolves the future."""
        bus = EventBusInmemory(environment="test", group="redeploy-test")
        await bus.start()

        target_corr_id = str(uuid4())
        other_corr_id = str(uuid4())
        handler = HandlerRedeployKafka(event_bus=bus, timeout_s=5.0)

        async def _agent_sends_wrong_then_right(message: object) -> None:
            # First publish an event with a different correlation_id
            wrong_completion = _make_completed_event(
                correlation_id=other_corr_id,
                git_sha="wrong",
            )
            await bus.publish(
                _EVT_TOPIC,
                key=other_corr_id.encode(),
                value=json.dumps(wrong_completion.model_dump(mode="json")).encode(),
            )
            # Then publish the right one
            right_completion = _make_completed_event(
                correlation_id=target_corr_id,
                git_sha="correct",
            )
            await bus.publish(
                _EVT_TOPIC,
                key=target_corr_id.encode(),
                value=json.dumps(right_completion.model_dump(mode="json")).encode(),
            )

        await bus.subscribe(
            _CMD_TOPIC, on_message=_agent_sends_wrong_then_right, group_id="fake-agent"
        )

        result = await handler.execute(scope="full", correlation_id=target_corr_id)

        assert result.success is True
        assert result.git_sha == "correct"

        await bus.close()

    async def test_make_completed_event_helper(self) -> None:
        """make_completion_event() produces a valid ModelDeployRebuildCompleted."""
        corr_id = str(uuid4())
        event = HandlerRedeployKafka.make_completion_event(
            correlation_id=corr_id,
            status="success",
            duration_seconds=45.2,
            git_sha="f00ba7",
            services_restarted=["svc-a", "svc-b"],
        )
        assert event.correlation_id == corr_id
        assert event.status == EnumRedeployStatus.SUCCESS
        assert event.duration_seconds == 45.2
        assert event.git_sha == "f00ba7"
        assert event.services_restarted == ["svc-a", "svc-b"]

    async def test_none_event_bus_raises(self) -> None:
        """Passing None as event_bus raises RuntimeError immediately."""
        with pytest.raises(RuntimeError, match="requires an event_bus"):
            HandlerRedeployKafka(event_bus=None)

    async def test_duration_from_agent_preferred(self) -> None:
        """Duration from deploy agent is used when > 0, else wall-clock."""
        bus = EventBusInmemory(environment="test", group="redeploy-test")
        await bus.start()

        corr_id = str(uuid4())
        handler = HandlerRedeployKafka(event_bus=bus, timeout_s=5.0)

        async def _agent_with_known_duration(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            completion = _make_completed_event(
                correlation_id=payload["correlation_id"],
                duration_seconds=123.456,
            )
            await bus.publish(
                _EVT_TOPIC,
                key=payload["correlation_id"].encode(),
                value=json.dumps(completion.model_dump(mode="json")).encode(),
            )

        await bus.subscribe(
            _CMD_TOPIC, on_message=_agent_with_known_duration, group_id="fake-agent"
        )

        result = await handler.execute(scope="full", correlation_id=corr_id)
        assert result.duration_seconds == 123.456

        await bus.close()

    async def test_command_payload_fields(self) -> None:
        """Published command payload contains all required deploy agent fields."""
        bus = EventBusInmemory(environment="test", group="redeploy-test")
        await bus.start()

        received_commands: list[dict[str, object]] = []
        corr_id = str(uuid4())
        handler = HandlerRedeployKafka(event_bus=bus, timeout_s=5.0)

        async def _capturing_agent(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            received_commands.append(payload)
            completion = _make_completed_event(correlation_id=payload["correlation_id"])
            await bus.publish(
                _EVT_TOPIC,
                key=payload["correlation_id"].encode(),
                value=json.dumps(completion.model_dump(mode="json")).encode(),
            )

        await bus.subscribe(
            _CMD_TOPIC, on_message=_capturing_agent, group_id="fake-agent"
        )

        await handler.execute(
            scope="runtime",
            git_ref="origin/develop",
            services=["omninode-runtime"],
            requested_by="test-suite",
            correlation_id=corr_id,
        )

        assert len(received_commands) == 1
        cmd = received_commands[0]
        assert cmd["correlation_id"] == corr_id
        assert cmd["scope"] == "runtime"
        assert cmd["git_ref"] == "origin/develop"
        assert cmd["services"] == ["omninode-runtime"]
        assert cmd["requested_by"] == "test-suite"

        await bus.close()
