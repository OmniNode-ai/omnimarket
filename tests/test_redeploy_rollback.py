"""Deliberate-failure rollback tests for node_redeploy (OMN-9579).

Verifies that when failures occur during or after deploy, the workflow
triggers rollback: restores the previous image, restarts services, and
emits a rolled_back event with the failure reason.

Prerequisite tickets needed before these tests can pass:
  1. Extract DeploymentAdapter protocol for injectable deploy/rollback
     actions (deploy, health_check, smoke_test, rollback, restart).
  2. Add previous_image / new_image tracking to ModelRedeployState.
  3. Implement rollback step in run_redeploy_workflow — when REBUILD
     or VERIFY_HEALTH fails, restore previous image and trigger restart.
  4. Add rollback event to contract.yaml:
     onex.evt.omnimarket.redeploy-rolled-back.v1
  5. Add failure injection for post-REBUILD phases so VERIFY_HEALTH can
     report smoke test / health check failures.

All tests marked xfail(strict=True) until rollback infrastructure exists.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_redeploy.handlers.handler_redeploy_kafka import (
    HandlerRedeployKafka,
)
from omnimarket.nodes.node_redeploy.handlers.handler_workflow_runner import (
    ModelRedeployWorkflowInput,
    run_redeploy_workflow,
)
from omnimarket.nodes.node_redeploy.models.model_deploy_agent_events import (
    EnumRedeployStatus,
    ModelDeployPhaseResults,
    ModelDeployRebuildCompleted,
    ModelRedeployResult,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_state import (
    EnumRedeployPhase,
)

_EVT_TOPIC = "onex.evt.deploy.rebuild-completed.v1"
_CMD_TOPIC = "onex.cmd.deploy.rebuild-requested.v1"
_ROLLBACK_TOPIC = "onex.evt.omnimarket.redeploy-rolled-back.v1"
_PREVIOUS_IMAGE = "omninode-runtime:v2.3.1"


def _make_completed_event(
    correlation_id: str,
    status: str = "success",
    git_sha: str = "abc123",
    services_restarted: list[str] | None = None,
    errors: list[str] | None = None,
) -> ModelDeployRebuildCompleted:
    return ModelDeployRebuildCompleted(
        correlation_id=correlation_id,
        status=EnumRedeployStatus(status),
        duration_seconds=10.0,
        git_sha=git_sha,
        services_restarted=services_restarted or ["omninode-runtime"],
        phase_results=ModelDeployPhaseResults(),
        errors=errors or [],
    )


@pytest.mark.unit
class TestRedeployRollback:
    """Deliberate-failure rollback tests for node_redeploy.

    Each test injects a specific failure mode and asserts that:
      1. Previous image is restored
      2. Restart is triggered
      3. The rollback event is emitted with the correct failure reason
    """

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Rollback not implemented — no mechanism to fail VERIFY_HEALTH, "
            "no DeploymentAdapter protocol, no rollback event. "
            "Needs: failure injection for post-REBUILD phases, "
            "DeploymentAdapter with rollback(), previous_image in state, "
            "onex.evt.omnimarket.redeploy-rolled-back.v1"
        ),
    )
    async def test_smoke_failure_after_successful_deploy(self) -> None:
        """REBUILD succeeds but smoke test fails -> rollback to previous image."""
        bus = EventBusInmemory(environment="test", group="rollback-test")
        await bus.start()

        corr_id = uuid4()
        rollback_events: list[dict] = []

        async def _on_rollback(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            rollback_events.append(payload)

        await bus.subscribe(
            _ROLLBACK_TOPIC, on_message=_on_rollback, group_id="rollback-capture"
        )

        async def _deploy_agent_success(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            completion = _make_completed_event(
                correlation_id=payload["correlation_id"],
                status="success",
                git_sha="newsha456",
            )
            await bus.publish(
                _EVT_TOPIC,
                key=payload["correlation_id"].encode(),
                value=json.dumps(completion.model_dump(mode="json")).encode(),
            )

        await bus.subscribe(
            _CMD_TOPIC, on_message=_deploy_agent_success, group_id="fake-agent"
        )

        workflow_input = ModelRedeployWorkflowInput(
            correlation_id=corr_id,
            scope="full",
            dry_run=False,
        )

        result = await run_redeploy_workflow(workflow_input, event_bus=bus)

        assert result.success is False
        assert len(rollback_events) == 1
        assert rollback_events[0]["correlation_id"] == str(corr_id)
        assert rollback_events[0].get("restored_image") == _PREVIOUS_IMAGE
        assert "smoke" in rollback_events[0].get("failure_reason", "").lower()

        await bus.close()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Rollback not implemented — deploy agent reports success with "
            "failing health_checks but workflow ignores them. "
            "Needs: health check verification in run_redeploy_workflow, "
            "DeploymentAdapter with rollback(), "
            "onex.evt.omnimarket.redeploy-rolled-back.v1"
        ),
    )
    async def test_health_check_failure_after_deploy(self) -> None:
        """Deploy succeeds but health checks fail -> rollback to previous image."""
        bus = EventBusInmemory(environment="test", group="rollback-test")
        await bus.start()

        corr_id = uuid4()
        rollback_events: list[dict] = []

        async def _on_rollback(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            rollback_events.append(payload)

        await bus.subscribe(
            _ROLLBACK_TOPIC, on_message=_on_rollback, group_id="rollback-capture"
        )

        async def _deploy_agent_unhealthy(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            completion = ModelDeployRebuildCompleted(
                correlation_id=payload["correlation_id"],
                status=EnumRedeployStatus.SUCCESS,
                duration_seconds=45.0,
                git_sha="badsha",
                services_restarted=["omninode-runtime"],
                phase_results=ModelDeployPhaseResults(),
                errors=[],
                health_checks=[
                    {
                        "service": "omninode-runtime",
                        "endpoint": "http://localhost:8085/health",
                        "status": "fail",
                        "latency_ms": 5000,
                    },
                ],
            )
            await bus.publish(
                _EVT_TOPIC,
                key=payload["correlation_id"].encode(),
                value=json.dumps(completion.model_dump(mode="json")).encode(),
            )

        await bus.subscribe(
            _CMD_TOPIC,
            on_message=_deploy_agent_unhealthy,
            group_id="fake-agent",
        )

        workflow_input = ModelRedeployWorkflowInput(
            correlation_id=corr_id,
            scope="full",
            dry_run=False,
        )

        result = await run_redeploy_workflow(workflow_input, event_bus=bus)

        assert result.success is False
        assert result.final_phase == EnumRedeployPhase.FAILED
        assert len(rollback_events) == 1
        assert rollback_events[0]["correlation_id"] == str(corr_id)
        assert rollback_events[0].get("restored_image") == _PREVIOUS_IMAGE
        assert "health" in rollback_events[0].get("failure_reason", "").lower()

        await bus.close()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Timeout rollback not implemented — HandlerRedeployKafka reports "
            "timeout in rebuild_result but no rollback to previous image. "
            "Needs: DeploymentAdapter with rollback(), previous_image in state, "
            "onex.evt.omnimarket.redeploy-rolled-back.v1"
        ),
    )
    async def test_timeout_during_deploy(self) -> None:
        """REBUILD times out -> rollback to previous known-good image."""
        bus = EventBusInmemory(environment="test", group="rollback-test")
        await bus.start()

        corr_id = uuid4()
        rollback_events: list[dict] = []

        async def _on_rollback(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            rollback_events.append(payload)

        await bus.subscribe(
            _ROLLBACK_TOPIC, on_message=_on_rollback, group_id="rollback-capture"
        )

        workflow_input = ModelRedeployWorkflowInput(
            correlation_id=corr_id,
            scope="full",
            dry_run=False,
        )

        mock_result = ModelRedeployResult(
            correlation_id=str(corr_id),
            success=False,
            status=EnumRedeployStatus.FAILED,
            duration_seconds=0.2,
            timed_out=True,
            errors=["Timed out after 0.1s waiting for deploy agent completion"],
        )

        async def _timeout_execute(
            *args: object, **kwargs: object
        ) -> ModelRedeployResult:
            return mock_result

        with patch.object(
            HandlerRedeployKafka, "execute", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.side_effect = _timeout_execute
            result = await run_redeploy_workflow(workflow_input, event_bus=bus)

        assert result.success is False
        assert result.rebuild_result is not None
        assert result.rebuild_result.timed_out is True
        assert len(rollback_events) == 1
        assert rollback_events[0]["correlation_id"] == str(corr_id)
        assert rollback_events[0].get("restored_image") == _PREVIOUS_IMAGE
        assert "timed out" in rollback_events[0].get("failure_reason", "").lower()

        await bus.close()
