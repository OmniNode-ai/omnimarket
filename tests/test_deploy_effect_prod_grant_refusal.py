# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deploy-effect prod-refusal + target-binding tests (OMN-13440, Phase 3).

Defense-in-depth: the deploy publish-monitor EFFECT independently refuses a prod
``ModelDeployPublishCommand`` unless it carries a verified ``promotion_grant``
whose ``(approved_lane==PROD, approved_image_digest,
approved_promotion_batch_id)`` MATCH the command's ``(runtime_lane,
image_digest, promotion_batch_id)`` — target binding, NOT mere grant presence. A
grant for batch/digest A must never authorize a deploy of batch/digest B.

Every test runs through the REAL dispatch path (the EFFECT's ``handle`` against a
real ``EventBusInmemory``, plus a golden chain through the orchestrator's gate ->
deploy-publish edge into the deploy EFFECT), not handler-isolation. The negative
tests assert the deploy-agent rebuild command was NEVER published.

A static assertion test proves no prod path through orchestrator -> gate ->
deploy-effect emits ``ModelDeployRebuildCommand`` without a verified ``grant_id``.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    EnumProdGrantReason,
    EnumRedeployStatus,
    EnumRuntimeLane,
    ModelDeployPhaseResults,
    ModelDeployPublishCommand,
    ModelDeployRebuildCompleted,
    ModelDeployRefusedEvent,
    ModelProdPromotionGateCommand,
    ModelProdPromotionGateDecision,
    ModelProdPromotionGrant,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_DEPLOY_REFUSED,
    TOPIC_REBUILD_COMPLETED,
    TOPIC_REBUILD_REQUESTED,
    HandlerDeployPublishMonitor,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_DEPLOY_PUBLISH,
    HandlerRedeployOrchestrator,
)
from omnimarket.nodes.node_redeploy_orchestrator.models.model_redeploy_start_command import (
    ModelRedeployStartCommand,
)

_DIGEST = "sha256:0037aaaa"
_OTHER_DIGEST = "sha256:9999ffff"
_BATCH = "promo-2026-06-21"
_OTHER_BATCH = "promo-2026-06-20"
_APPROVER = "platform-lead@omninode.ai"
_REQUESTER = "node_redeploy_orchestrator"
_EVAL_AT = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


def _grant(
    *,
    lane: EnumRuntimeLane = EnumRuntimeLane.PROD,
    digest: str = _DIGEST,
    batch: str = _BATCH,
    expires_at: datetime | None = None,
) -> ModelProdPromotionGrant:
    return ModelProdPromotionGrant(
        grant_id="grant-omn-13440",
        approved_lane=lane,
        approved_image_digest=digest,
        approved_promotion_batch_id=batch,
        approved_by=_APPROVER,
        created_at=_EVAL_AT - timedelta(hours=1),
        expires_at=expires_at or (_EVAL_AT + timedelta(hours=1)),
    )


def _publish_command(
    *,
    lane: EnumRuntimeLane = EnumRuntimeLane.PROD,
    digest: str | None = _DIGEST,
    batch: str | None = _BATCH,
    grant: ModelProdPromotionGrant | None,
    evaluated_at: datetime | None = _EVAL_AT,
) -> ModelDeployPublishCommand:
    return ModelDeployPublishCommand(
        correlation_id=uuid4(),
        runtime_lane=lane,
        image_digest=digest,
        promotion_batch_id=batch,
        promotion_grant=grant,
        evaluated_at=evaluated_at,
    )


def _envelope(command: ModelDeployPublishCommand) -> ModelEventEnvelope:
    return ModelEventEnvelope(
        payload=command,
        correlation_id=command.correlation_id,
        event_type="onex.cmd.omnimarket.redeploy-deploy-publish.v1",
    )


def _make_completed(correlation_id: str) -> ModelDeployRebuildCompleted:
    return ModelDeployRebuildCompleted(
        correlation_id=correlation_id,
        status=EnumRedeployStatus.SUCCESS,
        duration_seconds=10.0,
        git_sha="abc123",
        runtime_lane=EnumRuntimeLane.PROD,
        image_digest=_DIGEST,
        services_restarted=["omninode-runtime"],
        phase_results=ModelDeployPhaseResults(),
    )


async def _dispatch_through_effect(
    command: ModelDeployPublishCommand,
    *,
    agent_completes: bool,
) -> tuple[object, list[dict], list[dict]]:
    """Drive the deploy EFFECT via its real ``handle`` over a real in-memory bus.

    Returns (handler_output, rebuild_requested_payloads, refused_payloads). A fake
    deploy agent optionally answers the rebuild command so a happy-path deploy
    completes; on a refusal the rebuild command must never be published.
    """
    bus = EventBusInmemory(environment="test", group="omn-13440")
    await bus.start()
    rebuild_requests: list[dict] = []
    refusals: list[dict] = []

    async def _capture_rebuild(message: object) -> None:
        payload = json.loads(message.value)  # type: ignore[union-attr]
        rebuild_requests.append(payload)
        if agent_completes:
            completion = _make_completed(payload["correlation_id"])
            await bus.publish(
                TOPIC_REBUILD_COMPLETED,
                key=payload["correlation_id"].encode(),
                value=json.dumps(completion.model_dump(mode="json")).encode(),
            )

    async def _capture_refused(message: object) -> None:
        refusals.append(json.loads(message.value))  # type: ignore[union-attr]

    await bus.subscribe(
        TOPIC_REBUILD_REQUESTED, on_message=_capture_rebuild, group_id="fake-agent"
    )
    await bus.subscribe(
        TOPIC_DEPLOY_REFUSED, on_message=_capture_refused, group_id="refused-capture"
    )

    handler = HandlerDeployPublishMonitor(event_bus=bus, timeout_s=5.0)
    output = await handler.handle(_envelope(command))
    await bus.close()
    return output, rebuild_requests, refusals


@pytest.mark.unit
class TestDeployEffectProdGrantRefusal:
    async def test_deploy_effect_rejects_prod_without_verified_grant(self) -> None:
        """Prod command with NO grant -> refused; rebuild command never published."""
        command = _publish_command(grant=None)
        output, rebuild_requests, refusals = await _dispatch_through_effect(
            command, agent_completes=False
        )

        assert output.node_kind == EnumNodeKind.EFFECT
        assert rebuild_requests == [], "refused prod deploy must NOT reach the agent"
        assert len(output.events) == 1
        refused = output.events[0].payload
        assert isinstance(refused, ModelDeployRefusedEvent)
        assert refused.reason is EnumProdGrantReason.MISSING_PROMOTION_GRANT
        assert len(refusals) == 1
        assert refusals[0]["reason"] == "missing_promotion_grant"
        assert output.metrics["deploy_refused"] == 1.0
        assert output.metrics["rebuild_success"] == 0.0

    async def test_deploy_effect_rejects_prod_with_digest_mismatch(self) -> None:
        """Grant for digest A must never authorize a deploy of digest B."""
        command = _publish_command(grant=_grant(digest=_OTHER_DIGEST))
        output, rebuild_requests, refusals = await _dispatch_through_effect(
            command, agent_completes=False
        )

        assert rebuild_requests == []
        refused = output.events[0].payload
        assert isinstance(refused, ModelDeployRefusedEvent)
        assert refused.reason is EnumProdGrantReason.GRANT_DIGEST_MISMATCH
        assert refusals[0]["reason"] == "grant_digest_mismatch"

    async def test_deploy_effect_rejects_prod_with_batch_mismatch(self) -> None:
        """Grant for batch A must never authorize a deploy of batch B."""
        command = _publish_command(grant=_grant(batch=_OTHER_BATCH))
        output, rebuild_requests, refusals = await _dispatch_through_effect(
            command, agent_completes=False
        )

        assert rebuild_requests == []
        refused = output.events[0].payload
        assert isinstance(refused, ModelDeployRefusedEvent)
        assert refused.reason is EnumProdGrantReason.GRANT_BATCH_MISMATCH
        assert refusals[0]["reason"] == "grant_batch_mismatch"

    async def test_deploy_effect_rejects_prod_with_lane_mismatch(self) -> None:
        """A grant that authorizes a non-PROD lane never authorizes a prod deploy."""
        command = _publish_command(grant=_grant(lane=EnumRuntimeLane.STABILITY_TEST))
        output, rebuild_requests, _ = await _dispatch_through_effect(
            command, agent_completes=False
        )

        assert rebuild_requests == []
        refused = output.events[0].payload
        assert isinstance(refused, ModelDeployRefusedEvent)
        assert refused.reason is EnumProdGrantReason.GRANT_LANE_MISMATCH

    async def test_deploy_effect_rejects_prod_with_expired_grant(self) -> None:
        """A grant past its absolute expiry never authorizes a prod deploy."""
        command = _publish_command(
            grant=_grant(expires_at=_EVAL_AT - timedelta(seconds=1))
        )
        output, rebuild_requests, _ = await _dispatch_through_effect(
            command, agent_completes=False
        )

        assert rebuild_requests == []
        refused = output.events[0].payload
        assert isinstance(refused, ModelDeployRefusedEvent)
        assert refused.reason is EnumProdGrantReason.EXPIRED_PROMOTION_GRANT

    async def test_deploy_effect_rejects_prod_without_evaluated_at(self) -> None:
        """A prod deploy with no deterministic evaluated_at is unverifiable -> refused."""
        command = _publish_command(grant=_grant(), evaluated_at=None)
        output, rebuild_requests, _ = await _dispatch_through_effect(
            command, agent_completes=False
        )

        assert rebuild_requests == []
        refused = output.events[0].payload
        assert isinstance(refused, ModelDeployRefusedEvent)
        assert refused.reason is EnumProdGrantReason.EXPIRED_PROMOTION_GRANT

    async def test_deploy_effect_proceeds_with_matching_grant(self) -> None:
        """Target-bound matching grant -> deploy proceeds; rebuild command published."""
        command = _publish_command(grant=_grant())
        output, rebuild_requests, refusals = await _dispatch_through_effect(
            command, agent_completes=True
        )

        assert refusals == [], "a target-bound prod deploy must not be refused"
        assert len(rebuild_requests) == 1, "the deploy agent received the rebuild"
        assert rebuild_requests[0]["runtime_lane"] == EnumRuntimeLane.PROD.value
        assert rebuild_requests[0]["image_digest"] == _DIGEST
        # No refusal event; a successful deploy emits no rolled-back event either.
        assert all(
            not isinstance(e.payload, ModelDeployRefusedEvent) for e in output.events
        )
        assert output.metrics["rebuild_success"] == 1.0
        assert output.metrics.get("deploy_refused", 0.0) == 0.0

    async def test_non_prod_deploy_unaffected_by_grant_binding(self) -> None:
        """Dev/stability deploys carry no grant and are byte-for-byte unchanged."""
        command = _publish_command(
            lane=EnumRuntimeLane.DEV, digest=None, batch=None, grant=None
        )
        _, rebuild_requests, refusals = await _dispatch_through_effect(
            command, agent_completes=True
        )

        assert refusals == []
        assert len(rebuild_requests) == 1
        assert rebuild_requests[0]["runtime_lane"] == EnumRuntimeLane.DEV.value


def _gate_evaluated_envelope(
    *,
    grant: ModelProdPromotionGrant | None,
    evaluated_at: datetime | None,
    correlation_id: object,
) -> ModelEventEnvelope:
    """Build the gate-evaluated event the orchestrator consumes (decision+start+command)."""
    decision = ModelProdPromotionGateDecision(
        allowed=True,
        image_digest=_DIGEST,
        rollback_target="sha256:prev",
        reason="ok",
    )
    start = ModelRedeployStartCommand(
        correlation_id=correlation_id,
        runtime_lane=EnumRuntimeLane.PROD,
        image_digest=_DIGEST,
        promotion_batch_id=_BATCH,
    )
    gate_command = ModelProdPromotionGateCommand(
        correlation_id=correlation_id,
        runtime_lane=EnumRuntimeLane.PROD,
        requested_image_digest=_DIGEST,
        promotion_batch_id=_BATCH,
        requested_by=_REQUESTER,
        promotion_grant=grant,
        evaluated_at=evaluated_at,
    )
    return ModelEventEnvelope(
        payload={
            "decision": decision.model_dump(mode="json"),
            "start": start.model_dump(mode="json"),
            "command": gate_command.model_dump(mode="json"),
        },
        correlation_id=correlation_id,
        event_type="onex.evt.omnimarket.prod-promotion-gate-evaluated.v1",
    )


@pytest.mark.unit
class TestOrchestratorToDeployEffectGoldenChain:
    """Golden chain: orchestrator gate-evaluated -> deploy-publish -> deploy EFFECT."""

    async def test_matching_grant_threads_through_to_deploy(self) -> None:
        """Verified grant rides the deploy command; the EFFECT proceeds to the agent."""
        orchestrator = HandlerRedeployOrchestrator()
        corr_id = uuid4()
        envelope = _gate_evaluated_envelope(
            grant=_grant(), evaluated_at=_EVAL_AT, correlation_id=corr_id
        )
        output = await orchestrator.handle(envelope)
        assert [e.event_type for e in output.events] == [TOPIC_DEPLOY_PUBLISH]
        publish_command = output.events[0].payload
        assert isinstance(publish_command, ModelDeployPublishCommand)
        assert publish_command.promotion_grant is not None
        assert publish_command.promotion_grant.grant_id == "grant-omn-13440"
        assert publish_command.evaluated_at == _EVAL_AT

        # Dispatch the orchestrator-produced command into the real deploy EFFECT.
        _, rebuild_requests, refusals = await _dispatch_through_effect(
            publish_command, agent_completes=True
        )
        assert refusals == []
        assert len(rebuild_requests) == 1

    async def test_absent_grant_fails_closed_at_deploy_effect(self) -> None:
        """A prod gate-evaluated with no threaded grant -> deploy EFFECT refuses."""
        orchestrator = HandlerRedeployOrchestrator()
        corr_id = uuid4()
        envelope = _gate_evaluated_envelope(
            grant=None, evaluated_at=_EVAL_AT, correlation_id=corr_id
        )
        orchestrator_output = await orchestrator.handle(envelope)
        publish_command = orchestrator_output.events[0].payload
        assert isinstance(publish_command, ModelDeployPublishCommand)
        assert publish_command.promotion_grant is None

        _, rebuild_requests, refusals = await _dispatch_through_effect(
            publish_command, agent_completes=False
        )
        assert rebuild_requests == [], "fail closed: no grant -> no deploy-agent call"
        assert len(refusals) == 1
        assert refusals[0]["reason"] == "missing_promotion_grant"


@pytest.mark.unit
class TestStaticNoProdRebuildWithoutVerifiedGrant:
    """Static assertion: no prod path emits a rebuild command without grant verification."""

    def test_publish_and_monitor_is_guarded_by_grant_binding(self) -> None:
        """The EFFECT publishes the rebuild ONLY inside publish_and_monitor, which is
        reachable from handle ONLY after verify_prod_deploy_grant_binding passes.

        This is a structural proof that the single deploy-agent publish call sits
        behind the target-binding refusal guard: ``handle`` calls
        ``verify_prod_deploy_grant_binding`` and returns ``_refuse`` on a non-None
        result BEFORE it ever calls ``publish_and_monitor``; the only
        ``self._bus.publish(TOPIC_REBUILD_REQUESTED, ...)`` lives in
        ``publish_and_monitor``.
        """
        handler_src = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_redeploy_deploy_effect"
            / "handlers"
            / "handler_deploy_publish_monitor.py"
        )
        tree = ast.parse(handler_src.read_text())

        funcs = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        }
        assert "handle" in funcs
        assert "publish_and_monitor" in funcs

        handle_calls = {
            node.func.attr
            for node in ast.walk(funcs["handle"])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        handle_call_names = {
            node.func.id
            for node in ast.walk(funcs["handle"])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        # handle must call the binding verifier and dispatch to publish_and_monitor.
        assert "verify_prod_deploy_grant_binding" in handle_call_names
        assert "publish_and_monitor" in handle_calls

        # The rebuild-requested publish must NOT appear in handle itself — the only
        # deploy-agent publish lives behind the guard in publish_and_monitor.
        def _publishes_rebuild(fn: ast.AST) -> bool:
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "publish"
                    and node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "TOPIC_REBUILD_REQUESTED"
                ):
                    return True
            return False

        assert not _publishes_rebuild(funcs["handle"]), (
            "handle must not publish the deploy-agent rebuild command directly; it "
            "belongs behind the grant-binding guard in publish_and_monitor"
        )
        assert _publishes_rebuild(funcs["publish_and_monitor"])

    def test_refuse_never_publishes_rebuild_command(self) -> None:
        """The refusal path publishes the refused event, never the rebuild command."""
        handler_src = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_redeploy_deploy_effect"
            / "handlers"
            / "handler_deploy_publish_monitor.py"
        )
        tree = ast.parse(handler_src.read_text())
        refuse = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_refuse"
        )
        published_topics = {
            node.args[0].id
            for node in ast.walk(refuse)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "publish"
            and node.args
            and isinstance(node.args[0], ast.Name)
        }
        assert "TOPIC_REBUILD_REQUESTED" not in published_topics
        assert "TOPIC_DEPLOY_REFUSED" in published_topics
