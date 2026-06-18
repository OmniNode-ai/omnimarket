# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Orchestrator dispatch-boundary tests for the canonical redeploy (OMN-13211 / B3).

The redeploy orchestrator owns phase sequencing but dispatches commands OVER THE
BUS — it never runs an in-process FSM loop and never constructs sibling handlers.
``handle(envelope)`` returns ``ModelHandlerOutput.for_orchestrator`` carrying the
next bus command as an event envelope.

These tests pin the two routing edges:
  - ``redeploy-start`` -> the prod-promotion-gate-evaluate command;
  - ``prod-promotion-gate-evaluated`` -> the deploy-publish command (allowed) or
    the redeploy-completed:BLOCKED event (denied).

The orchestrator also tolerates dict / wrapped payloads from the runtime
auto-wiring without raising.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    EnumRedeployPhase,
    EnumRuntimeLane,
    ModelProdPromotionGateDecision,
    ModelRedeployCompletedEvent,
)
from omnimarket.nodes.node_redeploy_deploy_effect.models.model_deploy_publish_command import (
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_DEPLOY_PUBLISH,
    TOPIC_PROD_GATE_EVALUATE,
    TOPIC_REDEPLOY_COMPLETED,
    HandlerRedeployOrchestrator,
)
from omnimarket.nodes.node_redeploy_orchestrator.models.model_redeploy_start_command import (
    ModelRedeployStartCommand,
)

_DIGEST = "sha256:0037aaaa"


@pytest.mark.unit
class TestRedeployOrchestratorBoundary:
    async def test_start_emits_prod_gate_command(self) -> None:
        handler = HandlerRedeployOrchestrator()
        start = ModelRedeployStartCommand(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
        )
        envelope = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        output = await handler.handle(envelope)

        assert output.node_kind == EnumNodeKind.ORCHESTRATOR
        assert len(output.events) == 1
        emitted = output.events[0]
        assert emitted.event_type == TOPIC_PROD_GATE_EVALUATE
        # ORCHESTRATOR must not emit projections or set a result.
        assert output.projections == ()
        assert output.result is None

    async def test_start_accepts_dict_payload(self) -> None:
        handler = HandlerRedeployOrchestrator()
        corr_id = uuid4()
        envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={"correlation_id": str(corr_id), "runtime_lane": "dev"},
            correlation_id=corr_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        output = await handler.handle(envelope)
        assert len(output.events) == 1
        assert output.events[0].event_type == TOPIC_PROD_GATE_EVALUATE

    async def test_gate_allowed_emits_deploy_publish(self) -> None:
        handler = HandlerRedeployOrchestrator()
        corr_id = uuid4()
        decision = ModelProdPromotionGateDecision(
            allowed=True,
            image_digest=_DIGEST,
            rollback_target="sha256:prev",
            reason="ok",
        )
        start = ModelRedeployStartCommand(
            correlation_id=corr_id, runtime_lane=EnumRuntimeLane.PROD
        )
        envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={
                "decision": decision.model_dump(mode="json"),
                "start": start.model_dump(mode="json"),
            },
            correlation_id=corr_id,
            event_type="onex.evt.omnimarket.prod-promotion-gate-evaluated.v1",
        )
        output = await handler.handle(envelope)

        assert len(output.events) == 1
        emitted = output.events[0]
        assert emitted.event_type == TOPIC_DEPLOY_PUBLISH
        assert isinstance(emitted.payload, ModelDeployPublishCommand)
        assert emitted.payload.image_digest == _DIGEST
        assert emitted.payload.rollback_target == "sha256:prev"

    async def test_gate_blocked_emits_completed_blocked(self) -> None:
        handler = HandlerRedeployOrchestrator()
        corr_id = uuid4()
        decision = ModelProdPromotionGateDecision(
            allowed=False,
            image_digest=None,
            rollback_target=None,
            reason="prod digest does not match stability READY digest",
        )
        envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={"decision": decision.model_dump(mode="json")},
            correlation_id=corr_id,
            event_type="onex.evt.omnimarket.prod-promotion-gate-evaluated.v1",
        )
        output = await handler.handle(envelope)

        assert len(output.events) == 1
        emitted = output.events[0]
        assert emitted.event_type == TOPIC_REDEPLOY_COMPLETED
        assert isinstance(emitted.payload, ModelRedeployCompletedEvent)
        assert emitted.payload.final_phase == EnumRedeployPhase.BLOCKED
        assert "does not match" in (emitted.payload.error_message or "")
