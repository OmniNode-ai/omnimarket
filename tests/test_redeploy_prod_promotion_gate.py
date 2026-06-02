# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Production promotion gate tests for node_redeploy (OMN-12581, Phase 6).

Phase 6 makes a prod redeploy request depend on three deterministic facts,
resolved BEFORE any deploy-agent invocation:

  1. the reducer-owned readiness projection shows the stability-test lane is
     READY for the requested promotion batch and image digest;
  2. the OCC evidence PR is merged or the Receipt Gate has PASS evidence;
  3. a known rollback target (previous good digest) exists.

The exact stability READY ``image_digest`` is enforced — a prod request whose
digest differs from the latest stability-test READY digest is blocked before the
deploy agent is reached (the first-class regression guard for the live prod
0.36.1 vs stability 0.37.0 drift the baseline confirmed).

These tests are pure: they exercise the deterministic gate and the workflow's
prod entry point with no event bus and no deploy-agent invocation.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_redeploy.handlers.handler_workflow_runner import (
    ModelRedeployWorkflowInput,
    run_redeploy_workflow,
)
from omnimarket.nodes.node_redeploy.models.model_prod_promotion_gate import (
    EnumOccGateState,
    ModelProdPromotionGateDecision,
    ModelProdPromotionInputs,
    ModelReadinessProjectionFact,
    evaluate_prod_promotion_gate,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    EnumRuntimeLane,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_state import (
    EnumRedeployPhase,
)

_DIGEST_STABILITY = "sha256:0037aaaa"  # the 0.37.0 stability READY digest
_DIGEST_PROD_DRIFT = "sha256:0036bbbb"  # the live prod 0.36.1 drift digest
_BATCH = "promo-2026-06-02"
_ROLLBACK_TARGET = "sha256:0036bbbb"  # previous good = current prod digest


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


def _inputs(
    *,
    requested_digest: str = _DIGEST_STABILITY,
    batch: str = _BATCH,
    projection: ModelReadinessProjectionFact | None = None,
    occ_state: EnumOccGateState = EnumOccGateState.MERGED,
    rollback_target: str | None = _ROLLBACK_TARGET,
) -> ModelProdPromotionInputs:
    return ModelProdPromotionInputs(
        requested_image_digest=requested_digest,
        promotion_batch_id=batch,
        readiness_projection=projection
        if projection is not None
        else _ready_projection(),
        occ_gate_state=occ_state,
        rollback_target=rollback_target,
    )


# ---------------------------------------------------------------------------
# Deterministic prod promotion gate
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
        # No reducer-owned readiness projection exists for the request.
        inputs = ModelProdPromotionInputs(
            requested_image_digest=_DIGEST_STABILITY,
            promotion_batch_id=_BATCH,
            readiness_projection=None,
            occ_gate_state=EnumOccGateState.MERGED,
            rollback_target=_ROLLBACK_TARGET,
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
        # The requested prod digest differs from the latest stability READY
        # digest — the live 0.36.1 vs 0.37.0 drift regression guard.
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
        # Receipt Gate PASS is an accepted alternative to a merged OCC PR.
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
        first = evaluate_prod_promotion_gate(inputs)
        second = evaluate_prod_promotion_gate(inputs)
        assert first == second


# ---------------------------------------------------------------------------
# Workflow integration — prod gate runs before deploy-agent invocation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProdPromotionWorkflowGate:
    async def test_prod_digest_drift_blocked_before_deploy_agent(self) -> None:
        # A prod request whose digest != latest stability READY digest must be
        # BLOCKED with no deploy-agent invocation. event_bus=None proves the
        # gate decides before any Kafka rebuild command is published.
        projection = _ready_projection(digest=_DIGEST_STABILITY)
        workflow_input = ModelRedeployWorkflowInput(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.PROD,
            image_digest=_DIGEST_PROD_DRIFT,
            promotion_batch_id=_BATCH,
            readiness_projection=projection,
            occ_gate_state=EnumOccGateState.MERGED,
            rollback_target=_ROLLBACK_TARGET,
            dry_run=False,
        )
        result = await run_redeploy_workflow(workflow_input, event_bus=None)

        assert result.success is False
        assert result.final_phase == EnumRedeployPhase.BLOCKED
        assert result.rebuild_result is None
        assert result.error_message is not None
        assert "does not match" in result.error_message.lower()

    async def test_prod_without_occ_blocked_before_deploy_agent(self) -> None:
        projection = _ready_projection()
        workflow_input = ModelRedeployWorkflowInput(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.PROD,
            image_digest=_DIGEST_STABILITY,
            promotion_batch_id=_BATCH,
            readiness_projection=projection,
            occ_gate_state=EnumOccGateState.PENDING,
            rollback_target=_ROLLBACK_TARGET,
            dry_run=False,
        )
        result = await run_redeploy_workflow(workflow_input, event_bus=None)
        assert result.success is False
        assert result.final_phase == EnumRedeployPhase.BLOCKED
        assert result.rebuild_result is None

    async def test_prod_without_rollback_target_blocked(self) -> None:
        projection = _ready_projection()
        workflow_input = ModelRedeployWorkflowInput(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.PROD,
            image_digest=_DIGEST_STABILITY,
            promotion_batch_id=_BATCH,
            readiness_projection=projection,
            occ_gate_state=EnumOccGateState.MERGED,
            rollback_target=None,
            dry_run=False,
        )
        result = await run_redeploy_workflow(workflow_input, event_bus=None)
        assert result.success is False
        assert result.final_phase == EnumRedeployPhase.BLOCKED
        assert "rollback target" in (result.error_message or "").lower()

    async def test_prod_fully_eligible_passes_gate_then_runs_dry(self) -> None:
        # All Phase-6 conditions met: gate passes and the dry-run workflow walks
        # the FSM to DONE (no deploy agent in dry-run). The rollback target is
        # threaded into the workflow's previous_image for the rollback path.
        projection = _ready_projection()
        workflow_input = ModelRedeployWorkflowInput(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.PROD,
            image_digest=_DIGEST_STABILITY,
            promotion_batch_id=_BATCH,
            readiness_projection=projection,
            occ_gate_state=EnumOccGateState.MERGED,
            rollback_target=_ROLLBACK_TARGET,
            dry_run=True,
        )
        result = await run_redeploy_workflow(workflow_input, event_bus=None)
        assert result.success is True
        assert result.final_phase == EnumRedeployPhase.DONE

    async def test_prod_with_no_readiness_inputs_still_fails_closed(self) -> None:
        # Backward-compat: a prod request with NO Phase-6 inputs (no projection,
        # no occ state) still fails closed via the existing same-digest gate —
        # Phase 6 must not open a hole for un-gated prod deploys.
        workflow_input = ModelRedeployWorkflowInput(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.PROD,
            image_digest=_DIGEST_STABILITY,
            promotion_batch_id=_BATCH,
            dry_run=False,
        )
        result = await run_redeploy_workflow(workflow_input, event_bus=None)
        assert result.success is False
        assert result.final_phase == EnumRedeployPhase.BLOCKED
        assert result.rebuild_result is None
