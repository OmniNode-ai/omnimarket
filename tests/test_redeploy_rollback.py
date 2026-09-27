# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deliberate-failure rollback tests for the deploy effect (OMN-13211 / B3).

The rollback path re-homed from ``node_redeploy`` (DeploymentAdapterKafka +
workflow runner) into the canonical ``node_redeploy_deploy_effect``. A deploy
that succeeds then fails post-deploy verification (smoke / health / timeout)
restores the previous image and publishes
``onex.evt.omnimarket.redeploy-rolled-back.v1``. A deploy the agent reports as
failed is NOT a rollback (the artifact never went live).

Since OMN-18143 the command arm publishes and returns, and the rollback decision is
made by the effect's durable completion arm when the agent's completion arrives. Each
test therefore routes the agent's answers to that arm the way the runtime does
(``_DurableArms``), injects a specific failure mode, and asserts:
  1. the previous image is restored;
  2. the rolled-back event is emitted with the correct failure reason;
  3. the effect handler returns a rolled-back EFFECT event envelope;
  4. the handler publishes NOTHING to the rolled-back topic itself -- the
     runtime publishes the handler-output event, exactly once (OMN-16939).

(4) is the assertion this file was missing. It previously asserted
``len(rollback_events) == 1`` against the handler's OWN direct publish, which
was true in-memory and wrong on the runtime: there the direct publish and the
returned envelope both reached the topic, so every logical rollback landed
twice and the orchestrator terminalized twice.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.events.runtime_deployment import (
    DEFAULT_PREVIOUS_IMAGE,
    EnumRedeployStatus,
    EnumRuntimeLane,
    ModelDeployPhaseResults,
    ModelDeployRebuildCompleted,
    ModelHealthCheck,
    ModelRedeployRolledBackEvent,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_REBUILD_COMPLETED,
    TOPIC_REBUILD_REQUESTED,
    TOPIC_ROLLED_BACK,
    HandlerDeployPublishMonitor,
)
from omnimarket.nodes.node_redeploy_deploy_effect.models.model_deploy_publish_command import (
    ModelDeployPublishCommand,
)
from tests.test_omn19377_deploy_effect_skips_repeats import _DurableArms


def _make_completed(
    correlation_id: str,
    *,
    status: str = "success",
    git_sha: str = "abc123",
    errors: list[str] | None = None,
    health_checks: list[ModelHealthCheck] | None = None,
) -> ModelDeployRebuildCompleted:
    return ModelDeployRebuildCompleted(
        correlation_id=correlation_id,
        status=EnumRedeployStatus(status),
        duration_seconds=10.0,
        git_sha=git_sha,
        services_restarted=["omninode-runtime"],
        phase_results=ModelDeployPhaseResults(),
        errors=errors or [],
        health_checks=health_checks or [],
    )


def _envelope(command: ModelDeployPublishCommand) -> ModelEventEnvelope:
    return ModelEventEnvelope(
        payload=command,
        correlation_id=command.correlation_id,
        event_type="onex.cmd.omnimarket.redeploy-deploy-publish.v1",
    )


@pytest.mark.unit
class TestDeployEffectRollback:
    async def test_smoke_failure_after_successful_deploy(self) -> None:
        """REBUILD succeeds but smoke test fails -> rollback to previous image."""
        bus = EventBusInmemory(environment="test", group="rollback-test")
        await bus.start()
        arms = await _DurableArms(bus).start()
        corr_id = uuid4()
        rollback_events: list[dict] = []

        async def _on_rollback(message: object) -> None:
            rollback_events.append(json.loads(message.value))  # type: ignore[union-attr]

        await bus.subscribe(
            TOPIC_ROLLED_BACK, on_message=_on_rollback, group_id="rollback-capture"
        )

        async def _agent_success(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            completion = _make_completed(payload["correlation_id"], git_sha="newsha456")
            await bus.publish(
                TOPIC_REBUILD_COMPLETED,
                key=payload["correlation_id"].encode(),
                value=json.dumps(completion.model_dump(mode="json")).encode(),
            )

        await bus.subscribe(
            TOPIC_REBUILD_REQUESTED, on_message=_agent_success, group_id="fake-agent"
        )

        handler = HandlerDeployPublishMonitor(event_bus=bus)
        command = ModelDeployPublishCommand(
            correlation_id=corr_id,
            runtime_lane=EnumRuntimeLane.DEV,
            smoke_test=True,
        )
        published = await handler.handle(_envelope(command))
        assert published.events == ()
        (output,) = await arms.wait_for(1)

        assert output.node_kind == EnumNodeKind.EFFECT
        assert len(output.events) == 1
        rolled = output.events[0].payload
        assert isinstance(rolled, ModelRedeployRolledBackEvent)
        assert rolled.restored_image == DEFAULT_PREVIOUS_IMAGE
        assert "smoke" in rolled.failure_reason.lower()
        # OMN-16939: the handler must publish NOTHING to this topic itself.
        assert rollback_events == []

        await bus.close()

    async def test_health_check_failure_after_deploy(self) -> None:
        """Deploy succeeds but health checks fail -> rollback to previous image."""
        bus = EventBusInmemory(environment="test", group="rollback-test")
        await bus.start()
        arms = await _DurableArms(bus).start()
        corr_id = uuid4()
        rollback_events: list[dict] = []

        async def _on_rollback(message: object) -> None:
            rollback_events.append(json.loads(message.value))  # type: ignore[union-attr]

        await bus.subscribe(
            TOPIC_ROLLED_BACK, on_message=_on_rollback, group_id="rollback-capture"
        )

        async def _agent_unhealthy(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            completion = _make_completed(
                payload["correlation_id"],
                git_sha="badsha",
                health_checks=[
                    ModelHealthCheck(
                        service="omninode-runtime",
                        endpoint="http://localhost:8085/health",
                        status="fail",
                        latency_ms=5000,
                    )
                ],
            )
            await bus.publish(
                TOPIC_REBUILD_COMPLETED,
                key=payload["correlation_id"].encode(),
                value=json.dumps(completion.model_dump(mode="json")).encode(),
            )

        await bus.subscribe(
            TOPIC_REBUILD_REQUESTED, on_message=_agent_unhealthy, group_id="fake-agent"
        )

        handler = HandlerDeployPublishMonitor(event_bus=bus)
        command = ModelDeployPublishCommand(
            correlation_id=corr_id, runtime_lane=EnumRuntimeLane.DEV
        )
        published = await handler.handle(_envelope(command))
        assert published.events == ()
        (output,) = await arms.wait_for(1)

        assert len(output.events) == 1
        rolled = output.events[0].payload
        assert isinstance(rolled, ModelRedeployRolledBackEvent)
        assert rolled.restored_image == DEFAULT_PREVIOUS_IMAGE
        assert "health" in rolled.failure_reason.lower()
        assert rollback_events == []

        await bus.close()

    async def test_no_answer_yet_is_not_a_rollback(self) -> None:
        """No completion yet -> the dispatch returns and nothing is rolled back.

        This was a rollback while the command arm waited 600 s (OMN-18143): a real
        rebuild takes about 20 minutes, so the timeout called a healthy rebuild in
        progress a failure. The outcome now comes from the agent's own completion.
        """
        bus = EventBusInmemory(environment="test", group="rollback-test")
        await bus.start()
        rollback_events: list[dict] = []

        async def _on_rollback(message: object) -> None:
            rollback_events.append(json.loads(message.value))  # type: ignore[union-attr]

        await bus.subscribe(
            TOPIC_ROLLED_BACK, on_message=_on_rollback, group_id="rollback-capture"
        )
        # No deploy agent subscribed: nothing answers.

        handler = HandlerDeployPublishMonitor(event_bus=bus)
        command = ModelDeployPublishCommand(
            correlation_id=uuid4(), runtime_lane=EnumRuntimeLane.DEV
        )
        output = await handler.handle(_envelope(command))

        assert output.events == ()
        assert rollback_events == []

        await bus.close()

    async def test_agent_reported_failure_is_not_a_rollback(self) -> None:
        """A deploy the agent reports failed never went live -> no rollback."""
        bus = EventBusInmemory(environment="test", group="rollback-test")
        await bus.start()
        arms = await _DurableArms(bus).start()
        corr_id = uuid4()
        rollback_events: list[dict] = []

        async def _on_rollback(message: object) -> None:
            rollback_events.append(json.loads(message.value))  # type: ignore[union-attr]

        await bus.subscribe(
            TOPIC_ROLLED_BACK, on_message=_on_rollback, group_id="rollback-capture"
        )

        async def _agent_failed(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            completion = _make_completed(
                payload["correlation_id"],
                status="failed",
                errors=["docker build failed"],
            )
            await bus.publish(
                TOPIC_REBUILD_COMPLETED,
                key=payload["correlation_id"].encode(),
                value=json.dumps(completion.model_dump(mode="json")).encode(),
            )

        await bus.subscribe(
            TOPIC_REBUILD_REQUESTED, on_message=_agent_failed, group_id="fake-agent"
        )

        handler = HandlerDeployPublishMonitor(event_bus=bus)
        command = ModelDeployPublishCommand(
            correlation_id=corr_id, runtime_lane=EnumRuntimeLane.DEV
        )
        await handler.handle(_envelope(command))
        (output,) = await arms.wait_for(1)

        assert output.events == ()
        assert rollback_events == []

        await bus.close()
