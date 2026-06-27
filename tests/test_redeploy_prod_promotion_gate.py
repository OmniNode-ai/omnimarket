# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prod promotion gate tests — pure gate + canonical COMPUTE node (OMN-13211 / B3).

Phase 6 (OMN-12581) makes a prod redeploy request depend on three deterministic
facts, resolved BEFORE any deploy effect:

  1. the reducer-owned readiness projection shows the stability-test lane READY
     for the requested promotion batch and image digest;
  2. the OCC evidence PR is merged or the Receipt Gate has PASS evidence;
  3. a known rollback target (previous good digest) exists.

The exact stability READY ``image_digest`` is enforced — a prod request whose
digest differs from the latest stability-test READY digest is blocked before any
deploy effect (the first-class regression guard for the live prod 0.36.1 vs
stability 0.37.0 drift the baseline confirmed).

B3 re-expresses ``node_redeploy._evaluate_prod_gate`` as the canonical
``node_prod_promotion_gate_compute``. These tests exercise the pure gate
functions (now owned by ``omnimarket.events.runtime_deployment``) and the COMPUTE
handler that dispatches them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    EnumOccGateState,
    EnumProdGrantReason,
    EnumPromotionClass,
    EnumRuntimeLane,
    ModelProdPromotionGateDecision,
    ModelProdPromotionGrant,
    ModelProdPromotionInputs,
    ModelReadinessProjectionFact,
    evaluate_prod_promotion_gate,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.handlers.handler_prod_promotion_gate import (
    HandlerProdPromotionGate,
    evaluate_gate,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.models.model_prod_promotion_gate_command import (
    ModelProdPromotionGateCommand,
)

_DIGEST_STABILITY = "sha256:0037aaaa"  # the 0.37.0 stability READY digest
_DIGEST_PROD_DRIFT = "sha256:0036bbbb"  # the live prod 0.36.1 drift digest
_BATCH = "promo-2026-06-02"
_ROLLBACK_TARGET = "sha256:0036bbbb"  # previous good = current prod digest
_REQUESTER = "node_redeploy_orchestrator"  # gate command default requested_by
_APPROVER = "release-captain"
_EVALUATED_AT = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


def _ready_projection(
    *,
    digest: str = _DIGEST_STABILITY,
    batch: str = _BATCH,
    state: str = "READY",
) -> ModelReadinessProjectionFact:
    return ModelReadinessProjectionFact(
        runtime_lane=EnumRuntimeLane.STABILITY_TEST,
        readiness_state=state,
        image_digest=digest,
        promotion_batch_id=batch,
    )


def _valid_grant(
    *,
    digest: str = _DIGEST_STABILITY,
    batch: str = _BATCH,
) -> ModelProdPromotionGrant:
    """An approver-issued grant that authorizes the default prod request."""
    return ModelProdPromotionGrant(
        grant_id="grant-omn-13418-default",
        approved_lane=EnumRuntimeLane.PROD,
        approved_image_digest=digest,
        approved_promotion_batch_id=batch,
        approved_by=_APPROVER,
        created_at=_EVALUATED_AT - timedelta(minutes=5),
        expires_at=_EVALUATED_AT + timedelta(hours=2),
    )


def _inputs(
    *,
    requested_digest: str = _DIGEST_STABILITY,
    batch: str = _BATCH,
    projection: ModelReadinessProjectionFact | None = None,
    occ_state: EnumOccGateState = EnumOccGateState.MERGED,
    rollback_target: str | None = _ROLLBACK_TARGET,
    grant: ModelProdPromotionGrant | None = None,
) -> ModelProdPromotionInputs:
    return ModelProdPromotionInputs(
        requested_image_digest=requested_digest,
        promotion_batch_id=batch,
        readiness_projection=projection
        if projection is not None
        else _ready_projection(),
        occ_gate_state=occ_state,
        rollback_target=rollback_target,
        requested_by=_REQUESTER,
        promotion_grant=grant if grant is not None else _valid_grant(),
        evaluated_at=_EVALUATED_AT,
    )


def _command(
    *,
    runtime_lane: EnumRuntimeLane = EnumRuntimeLane.PROD,
    requested_digest: str | None = _DIGEST_STABILITY,
    batch: str | None = _BATCH,
    projection: ModelReadinessProjectionFact | None = None,
    occ_state: EnumOccGateState = EnumOccGateState.MERGED,
    rollback_target: str | None = _ROLLBACK_TARGET,
    grant: ModelProdPromotionGrant | None = None,
) -> ModelProdPromotionGateCommand:
    return ModelProdPromotionGateCommand(
        correlation_id=uuid4(),
        runtime_lane=runtime_lane,
        requested_image_digest=requested_digest,
        promotion_batch_id=batch,
        readiness_projection=projection,
        occ_gate_state=occ_state,
        rollback_target=rollback_target,
        requested_by=_REQUESTER,
        promotion_grant=grant if grant is not None else _valid_grant(),
        evaluated_at=_EVALUATED_AT,
    )


# ---------------------------------------------------------------------------
# Deterministic prod promotion gate (pure functions)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProdPromotionGate:
    def test_all_conditions_met_allows_and_reuses_digest(self) -> None:
        decision = evaluate_prod_promotion_gate(_inputs())
        assert isinstance(decision, ModelProdPromotionGateDecision)
        assert decision.allowed is True
        # prod reuses the exact stability READY digest — never rebuilds.
        assert decision.image_digest == _DIGEST_STABILITY
        assert decision.rollback_target == _ROLLBACK_TARGET

    def test_missing_readiness_projection_blocked(self) -> None:
        inputs = ModelProdPromotionInputs(
            requested_image_digest=_DIGEST_STABILITY,
            promotion_batch_id=_BATCH,
            readiness_projection=None,
            occ_gate_state=EnumOccGateState.MERGED,
            rollback_target=_ROLLBACK_TARGET,
            requested_by=_REQUESTER,
            promotion_grant=_valid_grant(),
            evaluated_at=_EVALUATED_AT,
        )
        decision = evaluate_prod_promotion_gate(inputs)
        assert decision.allowed is False
        assert "readiness projection" in decision.reason.lower()

    def test_stability_not_ready_blocked(self) -> None:
        decision = evaluate_prod_promotion_gate(
            _inputs(projection=_ready_projection(state="BLOCKED"))
        )
        assert decision.allowed is False
        assert "not ready" in decision.reason.lower()

    def test_digest_mismatch_blocked(self) -> None:
        # Requested prod digest differs from the latest stability READY digest —
        # the live 0.36.1 vs 0.37.0 drift regression guard.
        decision = evaluate_prod_promotion_gate(
            _inputs(requested_digest=_DIGEST_PROD_DRIFT)
        )
        assert decision.allowed is False
        assert "does not match" in decision.reason.lower()
        assert decision.image_digest is None

    def test_promotion_batch_mismatch_blocked(self) -> None:
        decision = evaluate_prod_promotion_gate(
            _inputs(projection=_ready_projection(batch="other-batch"))
        )
        assert decision.allowed is False
        assert "promotion batch" in decision.reason.lower()

    def test_occ_pending_blocked(self) -> None:
        decision = evaluate_prod_promotion_gate(
            _inputs(occ_state=EnumOccGateState.PENDING)
        )
        assert decision.allowed is False
        assert "occ" in decision.reason.lower()

    def test_occ_receipt_gate_pass_allows(self) -> None:
        decision = evaluate_prod_promotion_gate(
            _inputs(occ_state=EnumOccGateState.RECEIPT_GATE_PASS)
        )
        assert decision.allowed is True

    def test_missing_rollback_target_blocked(self) -> None:
        decision = evaluate_prod_promotion_gate(_inputs(rollback_target=None))
        assert decision.allowed is False
        assert "rollback target" in decision.reason.lower()

    def test_blank_rollback_target_blocked(self) -> None:
        decision = evaluate_prod_promotion_gate(_inputs(rollback_target="   "))
        assert decision.allowed is False
        assert "rollback target" in decision.reason.lower()

    def test_decision_is_stable_under_repeat(self) -> None:
        inputs = _inputs()
        assert evaluate_prod_promotion_gate(inputs) == evaluate_prod_promotion_gate(
            inputs
        )


# ---------------------------------------------------------------------------
# Canonical COMPUTE node — evaluate_gate + handler dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProdPromotionGateCompute:
    def test_non_prod_lane_allowed_unconditionally(self) -> None:
        for lane in (EnumRuntimeLane.DEV, EnumRuntimeLane.STABILITY_TEST):
            decision = evaluate_gate(
                _command(runtime_lane=lane, projection=None, rollback_target=None)
            )
            assert decision.allowed is True
            assert lane.value in decision.reason

    def test_prod_full_gate_allows_when_all_conditions_met(self) -> None:
        decision = evaluate_gate(_command(projection=_ready_projection()))
        assert decision.allowed is True
        assert decision.image_digest == _DIGEST_STABILITY

    def test_prod_digest_drift_blocked(self) -> None:
        decision = evaluate_gate(
            _command(
                requested_digest=_DIGEST_PROD_DRIFT,
                projection=_ready_projection(),
            )
        )
        assert decision.allowed is False
        assert "does not match" in decision.reason.lower()

    def test_prod_without_readiness_fails_closed(self) -> None:
        # No Phase-6 projection — falls back to the same-digest gate, fails closed.
        decision = evaluate_gate(_command(projection=None))
        assert decision.allowed is False

    async def test_handle_returns_compute_output(self) -> None:
        handler = HandlerProdPromotionGate()
        command = _command(projection=_ready_projection())
        envelope: ModelEventEnvelope[ModelProdPromotionGateCommand] = (
            ModelEventEnvelope(
                payload=command,
                correlation_id=command.correlation_id,
                event_type="onex.cmd.omnimarket.prod-promotion-gate-evaluate.v1",
            )
        )
        output = await handler.handle(envelope)

        assert output.node_kind == EnumNodeKind.COMPUTE
        assert isinstance(output.result, ModelProdPromotionGateDecision)
        assert output.result.allowed is True
        assert output.correlation_id == command.correlation_id
        # COMPUTE must not emit events/intents/projections.
        assert output.events == ()
        assert output.intents == ()
        assert output.projections == ()

    async def test_handle_accepts_dict_payload(self) -> None:
        handler = HandlerProdPromotionGate()
        command = _command(runtime_lane=EnumRuntimeLane.DEV, projection=None)
        envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload=command.model_dump(mode="json"),
            correlation_id=command.correlation_id,
        )
        output = await handler.handle(envelope)
        assert isinstance(output.result, ModelProdPromotionGateDecision)
        assert output.result.allowed is True


# ---------------------------------------------------------------------------
# OMN-13656 — stability-candidate / non-main-lineage refusal for prod
#
# A workspace-built (dev-HEAD-sibling) runtime image is stamped
# promotion_class=stability-candidate + non_main_lineage=true on its build
# manifest / OCI label. The prod-promotion gate REFUSES such an image for the
# prod lane absent an explicit candidate-authorizing grant. The refusal is a
# pure gate decision (a rejection) — provable with NO prod mutation.
# ---------------------------------------------------------------------------


def _candidate_grant(
    *,
    digest: str = _DIGEST_STABILITY,
    batch: str = _BATCH,
    authorizes_candidate: bool = True,
) -> ModelProdPromotionGrant:
    """A grant that (optionally) authorizes promoting a candidate image."""
    return ModelProdPromotionGrant(
        grant_id="grant-omn-13656-candidate",
        approved_lane=EnumRuntimeLane.PROD,
        approved_image_digest=digest,
        approved_promotion_batch_id=batch,
        approved_by=_APPROVER,
        created_at=_EVALUATED_AT - timedelta(minutes=5),
        expires_at=_EVALUATED_AT + timedelta(hours=2),
        authorizes_candidate=authorizes_candidate,
    )


@pytest.mark.unit
class TestStabilityCandidateProdRefusal:
    def test_candidate_class_refused_for_prod_without_grant(self) -> None:
        # Full readiness/OCC/rollback satisfied, default (non-candidate) grant —
        # a stability-candidate image is STILL refused for prod.
        decision = evaluate_prod_promotion_gate(
            _inputs().model_copy(
                update={"promotion_class": EnumPromotionClass.STABILITY_CANDIDATE}
            )
        )
        assert decision.allowed is False
        assert EnumProdGrantReason.CANDIDATE_NOT_AUTHORIZED.value in decision.reason
        assert "dev/stability only" in decision.reason
        assert decision.image_digest is None

    def test_non_main_lineage_refused_for_prod_without_grant(self) -> None:
        decision = evaluate_prod_promotion_gate(
            _inputs().model_copy(update={"non_main_lineage": True})
        )
        assert decision.allowed is False
        assert EnumProdGrantReason.CANDIDATE_NOT_AUTHORIZED.value in decision.reason

    def test_candidate_refused_even_without_readiness_projection(self) -> None:
        # Lineage gate runs FIRST: refusal does not depend on a readiness
        # projection being present (legacy un-gated path is also closed).
        decision = evaluate_prod_promotion_gate(
            ModelProdPromotionInputs(
                requested_image_digest=_DIGEST_STABILITY,
                promotion_batch_id=_BATCH,
                readiness_projection=None,
                occ_gate_state=EnumOccGateState.MERGED,
                rollback_target=_ROLLBACK_TARGET,
                requested_by=_REQUESTER,
                promotion_grant=_valid_grant(),
                promotion_class=EnumPromotionClass.STABILITY_CANDIDATE,
                non_main_lineage=True,
                evaluated_at=_EVALUATED_AT,
            )
        )
        assert decision.allowed is False
        assert EnumProdGrantReason.CANDIDATE_NOT_AUTHORIZED.value in decision.reason

    def test_candidate_allowed_with_explicit_candidate_grant(self) -> None:
        # An approver may opt in: a grant carrying authorizes_candidate=True lets
        # a candidate image through (the rest of the gate still applies).
        decision = evaluate_prod_promotion_gate(
            _inputs(grant=_candidate_grant()).model_copy(
                update={
                    "promotion_class": EnumPromotionClass.STABILITY_CANDIDATE,
                    "non_main_lineage": True,
                }
            )
        )
        assert decision.allowed is True
        assert decision.image_digest == _DIGEST_STABILITY

    def test_clean_main_unaffected_by_lineage_gate(self) -> None:
        # Default promotion_class is clean-main; existing behavior is unchanged.
        decision = evaluate_prod_promotion_gate(_inputs())
        assert decision.allowed is True

    def test_compute_handler_refuses_candidate_for_prod(self) -> None:
        command = _command(projection=_ready_projection()).model_copy(
            update={"promotion_class": EnumPromotionClass.STABILITY_CANDIDATE}
        )
        decision = evaluate_gate(command)
        assert decision.allowed is False
        assert EnumProdGrantReason.CANDIDATE_NOT_AUTHORIZED.value in decision.reason

    def test_compute_handler_refuses_candidate_no_projection(self) -> None:
        # The no-projection same-digest fallback path also refuses a candidate.
        command = _command(projection=None).model_copy(
            update={"non_main_lineage": True}
        )
        decision = evaluate_gate(command)
        assert decision.allowed is False
        assert EnumProdGrantReason.CANDIDATE_NOT_AUTHORIZED.value in decision.reason

    def test_compute_handler_allows_candidate_to_dev_lane(self) -> None:
        # A candidate image is pinnable to dev/stability: the dev lane is not
        # gated, so the candidate deploys there unconditionally.
        for lane in (EnumRuntimeLane.DEV, EnumRuntimeLane.STABILITY_TEST):
            command = _command(
                runtime_lane=lane, projection=None, rollback_target=None
            ).model_copy(
                update={
                    "promotion_class": EnumPromotionClass.STABILITY_CANDIDATE,
                    "non_main_lineage": True,
                }
            )
            decision = evaluate_gate(command)
            assert decision.allowed is True

    async def test_handle_emits_candidate_refusal_decision(self) -> None:
        # The refusal is observable as the COMPUTE node's decision result — the
        # gate-rejection "event" — with NO prod mutation involved.
        handler = HandlerProdPromotionGate()
        command = _command(projection=_ready_projection()).model_copy(
            update={
                "promotion_class": EnumPromotionClass.STABILITY_CANDIDATE,
                "non_main_lineage": True,
            }
        )
        envelope: ModelEventEnvelope[ModelProdPromotionGateCommand] = (
            ModelEventEnvelope(
                payload=command,
                correlation_id=command.correlation_id,
                event_type="onex.cmd.omnimarket.prod-promotion-gate-evaluate.v1",
            )
        )
        output = await handler.handle(envelope)
        assert isinstance(output.result, ModelProdPromotionGateDecision)
        assert output.result.allowed is False
        assert (
            EnumProdGrantReason.CANDIDATE_NOT_AUTHORIZED.value in output.result.reason
        )
        # COMPUTE purity preserved.
        assert output.events == ()
        assert output.intents == ()
        assert output.projections == ()
