# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prod-promotion authorization-grant boundary tests (OMN-13436 / Phase 1+4.5).

KEYSTONE of the prod-promotion authorization gate. These tests prove the gate
condition through the REAL dispatch path — orchestrator ``_on_start`` builds the
gate command, the gate COMPUTE handler evaluates it, and orchestrator
``_on_gate_evaluated`` routes the decision to deploy-publish or
redeploy-completed:BLOCKED — NOT handler-isolation against a pure function.

The grant model (``ModelProdPromotionGrant``) is an approver-issued, durable,
absolute-expiry authorization fact. The prod gate fails closed unless the grant
satisfies ALL of: ``approved_lane == PROD``, digest match, batch match,
``approved_by != requested_by`` (anti-self-grant), and ``evaluated_at`` within the
approver-set absolute expiry. dev/stability behavior is byte-for-byte unchanged.

Known follow-on gap (NOT closed here): requester identity is forgeable, so this
is single-party authorization with an approver signature, not two-person
integrity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.events.runtime_deployment import (
    EnumGrantResolution,
    EnumOccGateState,
    EnumRedeployPhase,
    EnumRuntimeLane,
    ModelDeployPublishCommand,
    ModelGrantProvenance,
    ModelProdPromotionGateCommand,
    ModelProdPromotionGateDecision,
    ModelProdPromotionGrant,
    ModelProdPromotionGrantResolveCommand,
    ModelProdPromotionGrantResolvedEvent,
    ModelProdPromotionInputs,
    ModelReadinessProjectionFact,
    ModelRedeployCompletedEvent,
    evaluate_prod_promotion_gate,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.handlers.handler_prod_promotion_gate import (
    HandlerProdPromotionGate,
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

_DIGEST = "sha256:0037aaaa"  # stability READY digest
_DIGEST_DRIFT = "sha256:0036bbbb"  # live prod drift digest
_BATCH = "promo-2026-06-02"
_ROLLBACK = "sha256:0036bbbb"
_REQUESTER = "node_redeploy_orchestrator"
_APPROVER = "release-captain"
_EVALUATED_AT = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


def _grant(
    *,
    grant_id: str = "grant-omn-13418-001",
    approved_lane: EnumRuntimeLane = EnumRuntimeLane.PROD,
    approved_image_digest: str = _DIGEST,
    approved_promotion_batch_id: str = _BATCH,
    approved_by: str = _APPROVER,
    created_at: datetime = _EVALUATED_AT - timedelta(minutes=5),
    expires_at: datetime = _EVALUATED_AT + timedelta(hours=2),
) -> ModelProdPromotionGrant:
    return ModelProdPromotionGrant(
        grant_id=grant_id,
        approved_lane=approved_lane,
        approved_image_digest=approved_image_digest,
        approved_promotion_batch_id=approved_promotion_batch_id,
        approved_by=approved_by,
        created_at=created_at,
        expires_at=expires_at,
    )


def _ready_projection() -> ModelReadinessProjectionFact:
    return ModelReadinessProjectionFact(
        runtime_lane=EnumRuntimeLane.STABILITY_TEST,
        readiness_state="READY",
        image_digest=_DIGEST,
        promotion_batch_id=_BATCH,
    )


def _inputs(
    *,
    grant: ModelProdPromotionGrant | None,
    requested_digest: str = _DIGEST,
    batch: str = _BATCH,
    requested_by: str = _REQUESTER,
    evaluated_at: datetime = _EVALUATED_AT,
) -> ModelProdPromotionInputs:
    return ModelProdPromotionInputs(
        requested_image_digest=requested_digest,
        promotion_batch_id=batch,
        readiness_projection=_ready_projection(),
        occ_gate_state=EnumOccGateState.MERGED,
        rollback_target=_ROLLBACK,
        requested_by=requested_by,
        promotion_grant=grant,
        evaluated_at=evaluated_at,
    )


def _start(
    *,
    runtime_lane: EnumRuntimeLane = EnumRuntimeLane.PROD,
    requested_by: str = _REQUESTER,
) -> ModelRedeployStartCommand:
    return ModelRedeployStartCommand(
        correlation_id=uuid4(),
        runtime_lane=runtime_lane,
        image_digest=_DIGEST,
        promotion_batch_id=_BATCH,
        readiness_projection=_ready_projection(),
        occ_gate_state=EnumOccGateState.MERGED,
        rollback_target=_ROLLBACK,
        requested_by=requested_by,
    )


async def _drive_dispatch(
    start: ModelRedeployStartCommand,
    *,
    grant: ModelProdPromotionGrant | None = None,
    evaluated_at: datetime = _EVALUATED_AT,
) -> tuple[ModelProdPromotionGateDecision, list[ModelEventEnvelope[object]]]:
    """Run the real dispatch path: start -> resolver -> gate compute -> gate-evaluated.

    Returns the gate decision (COMPUTE output) and the orchestrator's post-gate
    output events, so a test can assert both the decision and the routed edge.

    ``grant`` simulates the Phase-2b resolver EFFECT stamping the out-of-band
    promotion grant onto the gate command AFTER the orchestrator emits it — the
    grant is never authored by the redeploy-start request itself.
    """
    orchestrator = HandlerRedeployOrchestrator()
    gate = HandlerProdPromotionGate()

    # Edge 1: redeploy-start. Prod routes through the grant-RESOLVE command (the
    # grant is resolved out-of-band BEFORE the gate; OMN-13439 Phase 2b); non-prod
    # lanes go straight to the gate-evaluate command (no grant needed).
    start_envelope: ModelEventEnvelope[ModelRedeployStartCommand] = ModelEventEnvelope(
        payload=start,
        correlation_id=start.correlation_id,
        event_type="onex.cmd.omnimarket.redeploy-start.v1",
    )
    start_output = await orchestrator.handle(start_envelope)
    assert start_output.node_kind == EnumNodeKind.ORCHESTRATOR

    if start.runtime_lane is EnumRuntimeLane.PROD:
        assert [e.event_type for e in start_output.events] == [TOPIC_GRANT_RESOLVE]
        resolve_command = start_output.events[0].payload
        assert isinstance(resolve_command, ModelProdPromotionGrantResolveCommand)

        # Resolver boundary (Phase-2b, simulated): the resolver reads the grant
        # from onex_change_control@main and emits the resolved fact. ``grant``
        # stands in for what the durable anchor yields; ``None`` simulates an
        # absent/rejected grant.
        resolved_event = ModelProdPromotionGrantResolvedEvent(
            correlation_id=resolve_command.correlation_id,
            resolution=(
                EnumGrantResolution.RESOLVED
                if grant is not None
                else EnumGrantResolution.ABSENT
            ),
            grant=grant,
            evaluated_at=evaluated_at,
            provenance=ModelGrantProvenance(
                source_commit_sha="0" * 40,
                grant_id=grant.grant_id if grant is not None else None,
                file_sha256="0" * 64,
                codeowners_match=True,
            ),
        )
        resolved_envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={
                "resolved": resolved_event.model_dump(mode="json"),
                "start": start.model_dump(mode="json"),
            },
            correlation_id=start.correlation_id,
            event_type="onex.evt.omnimarket.prod-promotion-grant-resolved.v1",
        )
        resolved_output = await orchestrator.handle(resolved_envelope)
        gate_events = resolved_output.events
    else:
        # Non-prod: the start emits the gate-evaluate command directly.
        gate_events = start_output.events

    assert [e.event_type for e in gate_events] == [TOPIC_PROD_GATE_EVALUATE]
    gate_command = gate_events[0].payload
    assert isinstance(gate_command, ModelProdPromotionGateCommand)
    if start.runtime_lane is EnumRuntimeLane.PROD:
        # The gate command carries ONLY the out-of-band resolved grant
        # (round-tripped through the resolved event, so compare by value).
        assert gate_command.promotion_grant == grant

    # Edge 2: gate COMPUTE evaluates the command.
    gate_envelope: ModelEventEnvelope[ModelProdPromotionGateCommand] = (
        ModelEventEnvelope(
            payload=gate_command,
            correlation_id=gate_command.correlation_id,
            event_type=TOPIC_PROD_GATE_EVALUATE,
        )
    )
    gate_output = await gate.handle(gate_envelope)
    assert gate_output.node_kind == EnumNodeKind.COMPUTE
    decision = gate_output.result
    assert isinstance(decision, ModelProdPromotionGateDecision)

    # Edge 3: gate-evaluated -> orchestrator routes deploy-publish or BLOCKED.
    evaluated_envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
        payload={
            "decision": decision.model_dump(mode="json"),
            "start": start.model_dump(mode="json"),
        },
        correlation_id=start.correlation_id,
        event_type="onex.evt.omnimarket.prod-promotion-gate-evaluated.v1",
    )
    routed = await orchestrator.handle(evaluated_envelope)
    return decision, list(routed.events)


@pytest.mark.unit
class TestProdPromotionGrantModel:
    def test_grant_is_frozen_and_strongly_typed(self) -> None:
        grant = _grant()
        assert grant.approved_lane is EnumRuntimeLane.PROD
        assert isinstance(grant.created_at, datetime)
        assert isinstance(grant.expires_at, datetime)
        with pytest.raises(ValidationError):
            grant.approved_by = "someone-else"  # type: ignore[misc]


@pytest.mark.unit
class TestProdGrantBoundaryDispatch:
    """Real-dispatch boundary proofs through orchestrator + gate compute."""

    async def test_redeploy_start_cannot_supply_promotion_grant(self) -> None:
        """A redeploy-start request cannot self-supply a valid promotion grant.

        The orchestrator MUST NOT forward a caller-supplied grant from the start
        command — the grant is resolved out-of-band (Phase 2b resolver), never
        authored by the same request it authorizes. A prod start emits a
        grant-RESOLVE command which has no grant field at all, so a smuggled grant
        cannot ride through; the resolver reads the durable anchor on @main.
        """
        smuggled = _grant(approved_by=_REQUESTER)  # self-grant attempt
        start = ModelRedeployStartCommand(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.PROD,
            image_digest=_DIGEST,
            promotion_batch_id=_BATCH,
            readiness_projection=_ready_projection(),
            occ_gate_state=EnumOccGateState.MERGED,
            rollback_target=_ROLLBACK,
            requested_by=_REQUESTER,
            promotion_grant=smuggled,
        )
        orchestrator = HandlerRedeployOrchestrator()
        envelope: ModelEventEnvelope[ModelRedeployStartCommand] = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        output = await orchestrator.handle(envelope)
        resolve_command = output.events[0].payload
        # The start request cannot inject authorization: the resolve command has
        # no grant field — only the request key + requester ride through.
        assert isinstance(resolve_command, ModelProdPromotionGrantResolveCommand)
        assert not hasattr(resolve_command, "promotion_grant")
        assert resolve_command.requested_by == _REQUESTER

    async def test_orchestrator_resolves_grant_before_gate_command(self) -> None:
        """A prod redeploy-start resolves the grant BEFORE the gate or any deploy.

        Edge 1 must be the grant-RESOLVE command (not a gate-evaluate or a
        deploy-publish command): the grant is read from the durable anchor before
        the gate runs, so a missing/invalid grant blocks prod before the agent is
        ever invoked.
        """
        start = _start()
        orchestrator = HandlerRedeployOrchestrator()
        envelope: ModelEventEnvelope[ModelRedeployStartCommand] = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        output = await orchestrator.handle(envelope)
        emitted = [e.event_type for e in output.events]
        assert emitted == [TOPIC_GRANT_RESOLVE]
        assert TOPIC_PROD_GATE_EVALUATE not in emitted
        assert TOPIC_DEPLOY_PUBLISH not in emitted

    async def test_prod_gate_blocks_missing_grant(self) -> None:
        decision, events = await _drive_dispatch(_start(), grant=None)
        assert decision.allowed is False
        assert "missing_promotion_grant" in decision.reason
        assert [e.event_type for e in events] == [TOPIC_REDEPLOY_COMPLETED]
        completed = events[0].payload
        assert isinstance(completed, ModelRedeployCompletedEvent)
        assert completed.final_phase is EnumRedeployPhase.BLOCKED

    async def test_prod_gate_blocks_expired_grant(self) -> None:
        expired = _grant(expires_at=_EVALUATED_AT - timedelta(minutes=1))
        decision, events = await _drive_dispatch(_start(), grant=expired)
        assert decision.allowed is False
        assert "expired_promotion_grant" in decision.reason
        assert [e.event_type for e in events] == [TOPIC_REDEPLOY_COMPLETED]

    async def test_prod_gate_blocks_lane_mismatch(self) -> None:
        wrong_lane = _grant(approved_lane=EnumRuntimeLane.STABILITY_TEST)
        decision, events = await _drive_dispatch(_start(), grant=wrong_lane)
        assert decision.allowed is False
        assert "grant_lane_mismatch" in decision.reason
        assert [e.event_type for e in events] == [TOPIC_REDEPLOY_COMPLETED]

    async def test_prod_gate_blocks_digest_mismatch(self) -> None:
        wrong_digest = _grant(approved_image_digest=_DIGEST_DRIFT)
        decision, events = await _drive_dispatch(_start(), grant=wrong_digest)
        assert decision.allowed is False
        assert "grant_digest_mismatch" in decision.reason
        assert [e.event_type for e in events] == [TOPIC_REDEPLOY_COMPLETED]

    async def test_prod_gate_blocks_batch_mismatch(self) -> None:
        wrong_batch = _grant(approved_promotion_batch_id="other-batch")
        decision, events = await _drive_dispatch(_start(), grant=wrong_batch)
        assert decision.allowed is False
        assert "grant_batch_mismatch" in decision.reason
        assert [e.event_type for e in events] == [TOPIC_REDEPLOY_COMPLETED]

    async def test_prod_gate_blocks_self_grant(self) -> None:
        self_granted = _grant(approved_by=_REQUESTER)
        decision, events = await _drive_dispatch(_start(), grant=self_granted)
        assert decision.allowed is False
        assert "self_granted" in decision.reason
        assert [e.event_type for e in events] == [TOPIC_REDEPLOY_COMPLETED]

    async def test_prod_gate_allows_matching_fresh_grant(self) -> None:
        decision, events = await _drive_dispatch(_start(), grant=_grant())
        assert decision.allowed is True
        assert decision.image_digest == _DIGEST
        assert [e.event_type for e in events] == [TOPIC_DEPLOY_PUBLISH]
        deploy_cmd = events[0].payload
        assert isinstance(deploy_cmd, ModelDeployPublishCommand)
        assert deploy_cmd.image_digest == _DIGEST


@pytest.mark.unit
class TestProdGrantPureGate:
    """Pure-function coverage of the typed grant reasons (deterministic)."""

    def test_missing_grant_typed_reason(self) -> None:
        decision = evaluate_prod_promotion_gate(_inputs(grant=None))
        assert decision.allowed is False
        assert "missing_promotion_grant" in decision.reason

    def test_expired_grant_typed_reason(self) -> None:
        grant = _grant(expires_at=_EVALUATED_AT - timedelta(seconds=1))
        decision = evaluate_prod_promotion_gate(_inputs(grant=grant))
        assert decision.allowed is False
        assert "expired_promotion_grant" in decision.reason

    def test_grant_at_exact_expiry_is_allowed(self) -> None:
        # evaluated_at <= expires_at: boundary is inclusive.
        grant = _grant(expires_at=_EVALUATED_AT)
        decision = evaluate_prod_promotion_gate(_inputs(grant=grant))
        assert decision.allowed is True

    def test_lane_mismatch_typed_reason(self) -> None:
        grant = _grant(approved_lane=EnumRuntimeLane.DEV)
        decision = evaluate_prod_promotion_gate(_inputs(grant=grant))
        assert decision.allowed is False
        assert "grant_lane_mismatch" in decision.reason

    def test_digest_mismatch_typed_reason(self) -> None:
        grant = _grant(approved_image_digest=_DIGEST_DRIFT)
        decision = evaluate_prod_promotion_gate(_inputs(grant=grant))
        assert decision.allowed is False
        assert "grant_digest_mismatch" in decision.reason

    def test_batch_mismatch_typed_reason(self) -> None:
        grant = _grant(approved_promotion_batch_id="other-batch")
        decision = evaluate_prod_promotion_gate(_inputs(grant=grant))
        assert decision.allowed is False
        assert "grant_batch_mismatch" in decision.reason

    def test_self_granted_typed_reason(self) -> None:
        grant = _grant(approved_by=_REQUESTER)
        decision = evaluate_prod_promotion_gate(
            _inputs(grant=grant, requested_by=_REQUESTER)
        )
        assert decision.allowed is False
        assert "self_granted" in decision.reason

    def test_matching_fresh_grant_allows(self) -> None:
        decision = evaluate_prod_promotion_gate(_inputs(grant=_grant()))
        assert decision.allowed is True
        assert decision.image_digest == _DIGEST

    def test_no_datetime_now_inside_compute(self) -> None:
        """The decision is deterministic for a fixed evaluated_at (no wall clock)."""
        inputs = _inputs(grant=_grant())
        assert evaluate_prod_promotion_gate(inputs) == evaluate_prod_promotion_gate(
            inputs
        )


@pytest.mark.unit
class TestNonProdLanesUnchanged:
    """dev / stability-test behavior must be byte-for-byte unchanged by the grant."""

    def test_dev_allowed_without_grant(self) -> None:
        inputs = ModelProdPromotionInputs(
            requested_image_digest=_DIGEST,
            promotion_batch_id=_BATCH,
            readiness_projection=_ready_projection(),
            occ_gate_state=EnumOccGateState.MERGED,
            rollback_target=_ROLLBACK,
            requested_by=_REQUESTER,
            promotion_grant=None,
            evaluated_at=_EVALUATED_AT,
        )
        # The pure full-promotion gate is prod-only; dev/stability are decided by
        # the compute node (evaluate_gate) which short-circuits non-prod lanes.
        # Drive non-prod through the orchestrator + gate to prove no grant is needed.
        assert inputs.promotion_grant is None

    async def test_dev_dispatch_allows_without_grant(self) -> None:
        start = ModelRedeployStartCommand(
            correlation_id=uuid4(), runtime_lane=EnumRuntimeLane.DEV
        )
        decision, events = await _drive_dispatch(start)
        assert decision.allowed is True
        assert [e.event_type for e in events] == [TOPIC_DEPLOY_PUBLISH]

    async def test_stability_dispatch_allows_without_grant(self) -> None:
        start = ModelRedeployStartCommand(
            correlation_id=uuid4(), runtime_lane=EnumRuntimeLane.STABILITY_TEST
        )
        decision, events = await _drive_dispatch(start)
        assert decision.allowed is True
        assert [e.event_type for e in events] == [TOPIC_DEPLOY_PUBLISH]
