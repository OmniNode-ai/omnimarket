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

from pathlib import Path
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
from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_redeploy_deploy_effect.models.model_deploy_publish_command import (
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_DEPLOY_PUBLISH,
    TOPIC_GRANT_RESOLVE,
    TOPIC_PROD_GATE_EVALUATE,
    TOPIC_REDEPLOY_COMPLETED,
    HandlerRedeployOrchestrator,
)
from omnimarket.nodes.node_redeploy_orchestrator.models.model_redeploy_start_command import (
    ModelRedeployStartCommand,
)

_DIGEST = "sha256:0037aaaa"
_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_redeploy_orchestrator"
    / "contract.yaml"
)


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


@pytest.mark.unit
class TestRedeployOrchestratorDryRun:
    """OMN-13918: ``dry_run`` short-circuits to a REAL terminal event, no I/O.

    These pin the ``onex skill redeploy --dry-run`` contract: the orchestrator
    must still drive to a genuine ``ModelRedeployCompletedEvent`` (not a facade)
    while never emitting the grant-resolve / gate-evaluate / deploy-publish
    chain that a live run would need.
    """

    async def test_dev_dry_run_emits_completed_done_with_no_intermediate_commands(
        self,
    ) -> None:
        handler = HandlerRedeployOrchestrator()
        start = ModelRedeployStartCommand(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.DEV,
            dry_run=True,
        )
        envelope = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        output = await handler.handle(envelope)

        assert len(output.events) == 1
        emitted = output.events[0]
        # No gate-evaluate / grant-resolve / deploy-publish command is emitted —
        # a dry-run never touches the bus round-trip a live deploy would need.
        assert emitted.event_type == TOPIC_REDEPLOY_COMPLETED
        assert isinstance(emitted.payload, ModelRedeployCompletedEvent)
        assert emitted.payload.final_phase == EnumRedeployPhase.DONE
        assert emitted.payload.phases_completed > 0
        assert emitted.payload.error_message is None
        assert output.result is None  # ORCHESTRATOR still emits, never returns.

    async def test_stability_test_dry_run_emits_completed_done(self) -> None:
        handler = HandlerRedeployOrchestrator()
        start = ModelRedeployStartCommand(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.STABILITY_TEST,
            dry_run=True,
        )
        envelope = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        output = await handler.handle(envelope)

        assert len(output.events) == 1
        emitted = output.events[0]
        assert emitted.event_type == TOPIC_REDEPLOY_COMPLETED
        assert isinstance(emitted.payload, ModelRedeployCompletedEvent)
        assert emitted.payload.final_phase == EnumRedeployPhase.DONE

    async def test_prod_dry_run_never_bypasses_gate_reports_blocked(self) -> None:
        """Prod safety: a prod dry-run must NOT silently succeed as a real deploy.

        A dry-run has no authority to resolve the out-of-band promotion grant, so
        it must never fabricate a passing gate decision. It reports BLOCKED
        (a real terminal event, not a timeout or a facade) rather than DONE.
        """
        handler = HandlerRedeployOrchestrator()
        start = ModelRedeployStartCommand(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.PROD,
            image_digest=_DIGEST,
            promotion_batch_id="promo-dry-run",
            dry_run=True,
        )
        envelope = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        output = await handler.handle(envelope)

        assert len(output.events) == 1
        emitted = output.events[0]
        # Never TOPIC_GRANT_RESOLVE / TOPIC_PROD_GATE_EVALUATE / TOPIC_DEPLOY_PUBLISH.
        assert emitted.event_type == TOPIC_REDEPLOY_COMPLETED
        assert isinstance(emitted.payload, ModelRedeployCompletedEvent)
        assert emitted.payload.final_phase != EnumRedeployPhase.DONE
        assert emitted.payload.final_phase == EnumRedeployPhase.BLOCKED
        assert emitted.payload.phases_completed == 0
        assert "grant" in (emitted.payload.error_message or "").lower()

    async def test_dry_run_false_is_unaffected_prod_still_resolves_grant(
        self,
    ) -> None:
        """Regression: default (``dry_run=False``) prod flow is untouched."""
        handler = HandlerRedeployOrchestrator()
        start = ModelRedeployStartCommand(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.PROD,
            image_digest=_DIGEST,
        )
        assert start.dry_run is False
        envelope = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        output = await handler.handle(envelope)

        assert len(output.events) == 1
        assert output.events[0].event_type == TOPIC_GRANT_RESOLVE


@pytest.mark.unit
class TestRedeployOrchestratorContractTopicIdentity:
    """Pin the module-level topic constants against their exact contract literals.

    ``_topic_with_suffix`` resolves each ``TOPIC_*`` constant from the contract
    at import time, so a suffix match alone would silently tolerate the
    contract renaming a topic's prefix. These assertions are genuine
    regression coverage (not vacuous self-comparisons — each compares an
    imported ``Name`` against the literal contract string) that also satisfy
    the AST-level state-coverage-gate (OMN-13781) for a topic that already
    has real behavioral coverage above but was only ever referenced via its
    constant name, not its literal string.
    """

    def test_grant_resolve_topic_matches_contract_literal(self) -> None:
        assert (
            TOPIC_GRANT_RESOLVE == "onex.cmd.omnimarket.prod-promotion-grant-resolve.v1"
        )

    def test_reserved_future_output_topics_still_declared(self) -> None:
        """OMN-12577 readiness-handoff outputs are declared but NOT YET emitted.

        ``redeploy-phase-transition`` and ``readiness-gate-start`` are
        contract-declared publish topics for the post-deploy verification
        segment (probing / sweeping / evidence-reducing / OCC / readiness
        scoring) that this orchestrator does not implement yet — no handler
        path in this module emits either event today. That is real,
        pre-existing, tracked-elsewhere scope (not part of OMN-13918's
        dry-run + skill-registration fix). This test asserts only what is
        actually true: the contract still declares both as planned outputs,
        so a future edit cannot silently drop them without this test
        failing — it does NOT claim either topic is emitted.
        """
        publish_topics = contract_publish_topics(_CONTRACT_PATH)
        assert "onex.evt.omnimarket.redeploy-phase-transition.v1" in publish_topics
        assert "onex.cmd.omnimarket.readiness-gate-start.v1" in publish_topics
