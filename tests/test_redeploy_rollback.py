# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deliberate-failure rollback tests for the deploy effect (OMN-13211 / B3).

The rollback path re-homed from ``node_redeploy`` (DeploymentAdapterKafka +
workflow runner) into the canonical ``node_redeploy_deploy_effect``. A deploy
that succeeds then fails post-deploy verification (smoke / health / timeout)
restores the previous image and publishes
``onex.evt.omnimarket.redeploy-rolled-back.v1``. A deploy the agent reports as
failed is NOT a rollback (the artifact never went live).

Each test injects a specific failure mode and asserts:
  1. the previous image is restored;
  2. the rolled-back event is emitted with the correct failure reason;
  3. the effect handler returns a rolled-back EFFECT event envelope.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

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

        handler = HandlerDeployPublishMonitor(event_bus=bus, timeout_s=5.0)
        command = ModelDeployPublishCommand(
            correlation_id=corr_id,
            runtime_lane=EnumRuntimeLane.DEV,
            smoke_test=True,
        )
        output = await handler.handle(_envelope(command))

        assert output.node_kind == EnumNodeKind.EFFECT
        assert len(output.events) == 1
        rolled = output.events[0].payload
        assert isinstance(rolled, ModelRedeployRolledBackEvent)
        assert rolled.restored_image == DEFAULT_PREVIOUS_IMAGE
        assert "smoke" in rolled.failure_reason.lower()
        assert len(rollback_events) == 1
        assert rollback_events[0]["restored_image"] == DEFAULT_PREVIOUS_IMAGE

        await bus.close()

    async def test_health_check_failure_after_deploy(self) -> None:
        """Deploy succeeds but health checks fail -> rollback to previous image."""
        bus = EventBusInmemory(environment="test", group="rollback-test")
        await bus.start()
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

        handler = HandlerDeployPublishMonitor(event_bus=bus, timeout_s=5.0)
        command = ModelDeployPublishCommand(
            correlation_id=corr_id, runtime_lane=EnumRuntimeLane.DEV
        )
        output = await handler.handle(_envelope(command))

        assert len(output.events) == 1
        rolled = output.events[0].payload
        assert isinstance(rolled, ModelRedeployRolledBackEvent)
        assert rolled.restored_image == DEFAULT_PREVIOUS_IMAGE
        assert "health" in rolled.failure_reason.lower()
        assert len(rollback_events) == 1

        await bus.close()

    async def test_timeout_during_deploy_triggers_rollback(self) -> None:
        """REBUILD times out -> rollback to previous known-good image."""
        bus = EventBusInmemory(environment="test", group="rollback-test")
        await bus.start()
        corr_id = uuid4()
        rollback_events: list[dict] = []

        async def _on_rollback(message: object) -> None:
            rollback_events.append(json.loads(message.value))  # type: ignore[union-attr]

        await bus.subscribe(
            TOPIC_ROLLED_BACK, on_message=_on_rollback, group_id="rollback-capture"
        )
        # No deploy agent subscribed -> the publish-monitor times out.

        handler = HandlerDeployPublishMonitor(event_bus=bus, timeout_s=0.1)
        command = ModelDeployPublishCommand(
            correlation_id=corr_id, runtime_lane=EnumRuntimeLane.DEV
        )
        output = await handler.handle(_envelope(command))

        assert len(output.events) == 1
        rolled = output.events[0].payload
        assert isinstance(rolled, ModelRedeployRolledBackEvent)
        assert rolled.restored_image == DEFAULT_PREVIOUS_IMAGE
        assert "timed out" in rolled.failure_reason.lower()
        assert len(rollback_events) == 1

        await bus.close()

    async def test_agent_reported_failure_is_not_a_rollback(self) -> None:
        """A deploy the agent reports failed never went live -> no rollback."""
        bus = EventBusInmemory(environment="test", group="rollback-test")
        await bus.start()
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

        handler = HandlerDeployPublishMonitor(event_bus=bus, timeout_s=5.0)
        command = ModelDeployPublishCommand(
            correlation_id=corr_id, runtime_lane=EnumRuntimeLane.DEV
        )
        output = await handler.handle(_envelope(command))

        assert output.events == ()
        assert rollback_events == []

        await bus.close()
