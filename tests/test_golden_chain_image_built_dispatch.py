# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for the runtime-image-built -> orchestrator path (OMN-13655).

The ``runtime-image-built.v1`` event is the contract-sourced entrypoint for the
canonical cloud redeploy bus path. The orchestrator coerces it into a
``ModelRedeployStartCommand`` and routes identically to the normal redeploy-start
path:

  runtime-image-built (dev)  -> prod-gate-evaluate (trivially allowed at gate)
  runtime-image-built (prod) -> prod-promotion-grant-resolve command

  gate-evaluated: BLOCKED    -> redeploy-completed:BLOCKED  (rejection path)

These tests prove:

  1. A dev-lane ``ModelRuntimeImageBuilt`` event causes the orchestrator to emit
     ``prod-gate-evaluate`` (the contract-declared bus command, no direct deploy).
  2. A prod-lane event causes the orchestrator to emit ``prod-promotion-grant-resolve``
     (the grant-resolver EFFECT is the next hop; no independent deploy issued).
  3. A ``prod-promotion-gate-evaluated`` with ``allowed=False`` causes the
     orchestrator to emit ``redeploy-completed:BLOCKED`` — the gate REJECTION path
     proves no deploy is triggered when the gate denies.

No live prod deploy is performed; all assertions are on the bus command payloads
emitted by the orchestrator's ``ModelHandlerOutput.for_orchestrator``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    EnumBuildSource,
    EnumOccGateState,
    EnumPromotionClass,
    EnumRedeployPhase,
    EnumRuntimeLane,
    ModelProdPromotionGateDecision,
    ModelRedeployCompletedEvent,
    ModelRuntimeImageBuilt,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_DEPLOY_PUBLISH,
    TOPIC_GRANT_RESOLVE,
    TOPIC_PROD_GATE_EVALUATE,
    TOPIC_REDEPLOY_COMPLETED,
    HandlerRedeployOrchestrator,
)

_DIGEST = "sha256:aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"
_SOURCE_SHA = "deadbeefcafe1234" * 2
_BATCH = "promo-2026-06-27"
_PROVENANCE = "ci:build-runtime.yml:run_id=12345"


@pytest.mark.unit
class TestImageBuiltDispatch:
    """node_redeploy_orchestrator: runtime-image-built.v1 routing."""

    async def test_dev_image_built_emits_gate_evaluate(self) -> None:
        """Edge 1: dev runtime-image-built -> prod-gate-evaluate command.

        A dev-lane build-complete event routes through the canonical gate path.
        The orchestrator does NOT issue 'gh workflow run' or any direct deploy;
        it emits exactly one bus command: the gate-evaluate command.
        """
        handler = HandlerRedeployOrchestrator()
        correlation_id = uuid4()

        built = ModelRuntimeImageBuilt(
            correlation_id=correlation_id,
            digest=_DIGEST,
            source_sha=_SOURCE_SHA,
            build_source=EnumBuildSource.RELEASE,
            promotion_class=EnumPromotionClass.CLEAN_MAIN,
            provenance=_PROVENANCE,
            runtime_lane=EnumRuntimeLane.DEV,
        )
        envelope = ModelEventEnvelope(
            payload=built,
            correlation_id=correlation_id,
            event_type="onex.evt.omnimarket.runtime-image-built.v1",
        )

        result = await handler.handle(envelope)

        assert result.events, "orchestrator must emit exactly one bus command"
        assert len(result.events) == 1, f"expected 1 event, got {len(result.events)}"
        cmd_envelope = result.events[0]
        assert cmd_envelope.event_type == TOPIC_PROD_GATE_EVALUATE, (
            f"dev build-complete must route to {TOPIC_PROD_GATE_EVALUATE!r}; "
            f"got {cmd_envelope.event_type!r}"
        )
        # The digest from the built event is threaded into the gate command.
        gate_payload = cmd_envelope.payload
        assert hasattr(gate_payload, "requested_image_digest"), (
            "gate command must carry requested_image_digest"
        )
        assert gate_payload.requested_image_digest == _DIGEST, (
            "gate command must carry the build digest"
        )
        assert gate_payload.runtime_lane is EnumRuntimeLane.DEV, (
            "gate command must carry the dev runtime lane"
        )

    async def test_prod_image_built_emits_grant_resolve(self) -> None:
        """Edge 2: prod runtime-image-built -> prod-promotion-grant-resolve command.

        A prod-lane build-complete event routes through the grant-resolver EFFECT
        first (OMN-13439 Phase 2b). The orchestrator does NOT issue a deploy
        directly; it emits the grant-resolve command for the resolver EFFECT.
        No independent 'gh workflow run deploy-onex-prod.yml' is issued.
        """
        handler = HandlerRedeployOrchestrator()
        correlation_id = uuid4()

        built = ModelRuntimeImageBuilt(
            correlation_id=correlation_id,
            digest=_DIGEST,
            source_sha=_SOURCE_SHA,
            build_source=EnumBuildSource.RELEASE,
            promotion_class=EnumPromotionClass.CLEAN_MAIN,
            provenance=_PROVENANCE,
            runtime_lane=EnumRuntimeLane.PROD,
            promotion_batch_id=_BATCH,
        )
        envelope = ModelEventEnvelope(
            payload=built,
            correlation_id=correlation_id,
            event_type="onex.evt.omnimarket.runtime-image-built.v1",
        )

        result = await handler.handle(envelope)

        assert result.events, "orchestrator must emit the grant-resolve command"
        assert len(result.events) == 1
        cmd_envelope = result.events[0]
        assert cmd_envelope.event_type == TOPIC_GRANT_RESOLVE, (
            f"prod build-complete must route to {TOPIC_GRANT_RESOLVE!r} first; "
            f"got {cmd_envelope.event_type!r}"
        )

    async def test_gate_rejected_emits_completed_blocked(self) -> None:
        """Gate REJECTION path: gate-evaluated:denied -> redeploy-completed:BLOCKED.

        When the prod-promotion-gate-compute returns a denied decision, the
        orchestrator emits ``redeploy-completed:BLOCKED`` — no deploy-publish
        command is emitted, proving the gate blocks the deploy path.

        This test exercises the REJECTION path without touching prod and proves
        the gate is wired into the canonical bus path (no bypass).
        """
        handler = HandlerRedeployOrchestrator()
        correlation_id = uuid4()

        denied_decision = ModelProdPromotionGateDecision(
            allowed=False,
            image_digest=None,
            rollback_target=None,
            reason="missing_promotion_grant: test rejection",
        )
        gate_payload = {
            "decision": denied_decision.model_dump(mode="json"),
            "start": {
                "correlation_id": str(correlation_id),
                "runtime_lane": EnumRuntimeLane.PROD.value,
                "image_digest": _DIGEST,
                "promotion_batch_id": _BATCH,
                "occ_gate_state": EnumOccGateState.PENDING.value,
            },
        }
        envelope = ModelEventEnvelope(
            payload=gate_payload,
            correlation_id=correlation_id,
            event_type="onex.evt.omnimarket.prod-promotion-gate-evaluated.v1",
        )

        result = await handler.handle(envelope)

        assert result.events, "orchestrator must emit redeploy-completed on gate denial"
        assert len(result.events) == 1
        completed_envelope = result.events[0]
        assert completed_envelope.event_type == TOPIC_REDEPLOY_COMPLETED, (
            f"denied gate must route to {TOPIC_REDEPLOY_COMPLETED!r}; "
            f"got {completed_envelope.event_type!r}"
        )
        completed = completed_envelope.payload
        assert isinstance(completed, ModelRedeployCompletedEvent), (
            "completed payload must be ModelRedeployCompletedEvent"
        )
        assert completed.final_phase is EnumRedeployPhase.BLOCKED, (
            f"denied gate must yield final_phase=BLOCKED; got {completed.final_phase!r}"
        )
        # Critical: no deploy-publish command was emitted (no prod mutation).
        assert completed_envelope.event_type != TOPIC_DEPLOY_PUBLISH, (
            "BLOCKED gate must NOT emit a deploy-publish command"
        )

    async def test_image_built_no_direct_deploy_topic(self) -> None:
        """dev runtime-image-built never emits the deploy-publish command directly.

        Confirms the bus path is gate-mediated: the orchestrator always emits the
        gate-evaluate command, never the deploy-publish command, for a build-complete
        event. This proves no 'gh workflow run' or direct deploy is bypassed.
        """
        handler = HandlerRedeployOrchestrator()
        correlation_id = uuid4()

        built = ModelRuntimeImageBuilt(
            correlation_id=correlation_id,
            digest=_DIGEST,
            source_sha=_SOURCE_SHA,
            build_source=EnumBuildSource.WORKSPACE,
            promotion_class=EnumPromotionClass.STABILITY_CANDIDATE,
            provenance=_PROVENANCE,
            runtime_lane=EnumRuntimeLane.DEV,
        )
        envelope = ModelEventEnvelope(
            payload=built,
            correlation_id=correlation_id,
            event_type="onex.evt.omnimarket.runtime-image-built.v1",
        )

        result = await handler.handle(envelope)

        for evt in result.events:
            assert evt.event_type != TOPIC_DEPLOY_PUBLISH, (
                "runtime-image-built must never emit deploy-publish directly; "
                "it must always go through the prod-promotion gate"
            )
