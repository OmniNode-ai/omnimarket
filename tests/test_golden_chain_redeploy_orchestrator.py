# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for node_redeploy_orchestrator (OMN-13211 / B3).

The redeploy ORCHESTRATOR dispatches commands over the bus; it has no in-process
FSM loop. Its transitions are the routing edges:

  redeploy-start                  -> prod-promotion-gate-evaluate (COMPUTE command)
  prod-promotion-gate-evaluated   -> redeploy-deploy-publish      (EFFECT command, allowed)
  prod-promotion-gate-evaluated   -> redeploy-completed:BLOCKED   (denied)

# hand-authored: FSM schema not yet on orchestrators (verdict b)
Per the §6 golden-chain DoD, orchestrators have NO typed FSM field today, so
contract-derived auto-generation is not available; these chains are
HAND-AUTHORED full-transition coverage and must be re-generated once the
orchestrator typed-FSM schema (§6 option (a)) lands.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    EnumOccGateState,
    EnumRedeployPhase,
    EnumRuntimeLane,
    ModelDeployPublishCommand,
    ModelProdPromotionGateCommand,
    ModelProdPromotionGateDecision,
    ModelReadinessProjectionFact,
    ModelRedeployCompletedEvent,
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
_BATCH = "promo-2026-06-02"


@pytest.mark.unit
class TestRedeployOrchestratorGoldenChain:
    async def test_dev_start_to_gate_evaluate(self) -> None:
        """Edge 1 (dev): redeploy-start -> prod-gate-evaluate command."""
        handler = HandlerRedeployOrchestrator()
        start = ModelRedeployStartCommand(
            correlation_id=uuid4(), runtime_lane=EnumRuntimeLane.DEV
        )
        envelope = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        output = await handler.handle(envelope)

        assert output.node_kind == EnumNodeKind.ORCHESTRATOR
        assert [e.event_type for e in output.events] == [TOPIC_PROD_GATE_EVALUATE]
        gate_cmd = output.events[0].payload
        assert isinstance(gate_cmd, ModelProdPromotionGateCommand)
        assert gate_cmd.runtime_lane is EnumRuntimeLane.DEV

    async def test_prod_start_to_gate_evaluate_threads_facts(self) -> None:
        """Edge 1 (prod): start threads readiness/OCC/rollback into the gate command."""
        handler = HandlerRedeployOrchestrator()
        projection = ModelReadinessProjectionFact(
            readiness_state="READY", image_digest=_DIGEST, promotion_batch_id=_BATCH
        )
        start = ModelRedeployStartCommand(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.PROD,
            image_digest=_DIGEST,
            promotion_batch_id=_BATCH,
            readiness_projection=projection,
            occ_gate_state=EnumOccGateState.MERGED,
            rollback_target="sha256:prev",
        )
        envelope = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        output = await handler.handle(envelope)
        gate_cmd = output.events[0].payload
        assert isinstance(gate_cmd, ModelProdPromotionGateCommand)
        assert gate_cmd.readiness_projection == projection
        assert gate_cmd.occ_gate_state is EnumOccGateState.MERGED
        assert gate_cmd.rollback_target == "sha256:prev"

    async def test_gate_allowed_to_deploy_publish(self) -> None:
        """Edge 2 (allowed): gate-evaluated -> deploy-publish command."""
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
        assert [e.event_type for e in output.events] == [TOPIC_DEPLOY_PUBLISH]
        deploy_cmd = output.events[0].payload
        assert isinstance(deploy_cmd, ModelDeployPublishCommand)
        assert deploy_cmd.image_digest == _DIGEST

    async def test_gate_blocked_to_completed_blocked(self) -> None:
        """Edge 3 (denied): gate-evaluated -> redeploy-completed:BLOCKED."""
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
        assert [e.event_type for e in output.events] == [TOPIC_REDEPLOY_COMPLETED]
        completed = output.events[0].payload
        assert isinstance(completed, ModelRedeployCompletedEvent)
        assert completed.final_phase is EnumRedeployPhase.BLOCKED

    async def test_orchestrator_emits_no_projections_or_result(self) -> None:
        """ORCHESTRATOR output constraint: events/intents only."""
        handler = HandlerRedeployOrchestrator()
        start = ModelRedeployStartCommand(correlation_id=uuid4())
        envelope = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        output = await handler.handle(envelope)
        assert output.projections == ()
        assert output.result is None
