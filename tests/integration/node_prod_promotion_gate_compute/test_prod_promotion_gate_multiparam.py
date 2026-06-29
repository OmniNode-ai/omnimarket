# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_prod_promotion_gate_compute (OMN-13683, WS-5 Wave 9).

Variant A (COMPUTE): drives the real ``HandlerProdPromotionGate.handle`` dispatch
end-to-end over a ``ModelEventEnvelope`` and asserts the typed
``ModelProdPromotionGateDecision`` carried on ``ModelHandlerOutput`` for a matrix
of distinct gate-fact param sets. This is the prod-promotion *gate sub-node ONLY*
— it is a pure, in-process decision (a rejection or an allow). It NEVER exercises
a live deploy EFFECT, live prod, or the .201 server.

Surface covered (each a parametrized case, >=1 negative control):
  * non-prod lanes (dev / stability) -> ALLOW unconditionally (gate is a no-op);
  * prod + full valid facts (MERGED / RECEIPT_GATE_PASS) -> ALLOW, digest reused;
  * prod BLOCK modes: no readiness projection, not-READY, digest drift, batch
    mismatch, OCC pending, missing/blank rollback, missing grant, self-granted,
    expired grant, grant lane mismatch, stability-candidate / non-main lineage;
  * UNKNOWN-fail-closed: ``prod_health`` resolved UNKNOWN with no grant still
    BLOCKS. NOTE: this COMPUTE node does NOT consult ``prod_health`` — the
    health-conditional recovery waiver lives in the resolver EFFECT /
    orchestrator, not here — so prod_health=UNHEALTHY *without* a grant also
    fails CLOSED at this node. The cases below assert that real behavior rather
    than a waiver that this node does not implement.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    EnumOccGateState,
    EnumProdGrantReason,
    EnumProdHealth,
    EnumPromotionClass,
    EnumRuntimeLane,
    ModelProdHealthFact,
    ModelProdPromotionGateDecision,
    ModelProdPromotionGrant,
    ModelReadinessProjectionFact,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.handlers.handler_prod_promotion_gate import (
    HandlerProdPromotionGate,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.models.model_prod_promotion_gate_command import (
    ModelProdPromotionGateCommand,
)

_DIGEST_STABILITY = "sha256:0037aaaa"  # stability READY digest
_DIGEST_PROD_DRIFT = "sha256:0036bbbb"  # live prod drift digest
_BATCH = "promo-2026-06-02"
_ROLLBACK_TARGET = "sha256:0036bbbb"
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


def _grant(
    *,
    digest: str = _DIGEST_STABILITY,
    batch: str = _BATCH,
    approved_by: str = _APPROVER,
    approved_lane: EnumRuntimeLane = EnumRuntimeLane.PROD,
    expires_delta: timedelta = timedelta(hours=2),
    authorizes_candidate: bool = False,
) -> ModelProdPromotionGrant:
    return ModelProdPromotionGrant(
        grant_id="grant-omn-13683",
        approved_lane=approved_lane,
        approved_image_digest=digest,
        approved_promotion_batch_id=batch,
        approved_by=approved_by,
        created_at=_EVALUATED_AT - timedelta(minutes=5),
        expires_at=_EVALUATED_AT + expires_delta,
        authorizes_candidate=authorizes_candidate,
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
    promotion_class: EnumPromotionClass = EnumPromotionClass.CLEAN_MAIN,
    non_main_lineage: bool = False,
    prod_health: ModelProdHealthFact | None = None,
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
        promotion_grant=grant if grant is not None else _grant(),
        promotion_class=promotion_class,
        non_main_lineage=non_main_lineage,
        evaluated_at=_EVALUATED_AT,
        prod_health=prod_health,
    )


def _health(state: EnumProdHealth) -> ModelProdHealthFact:
    return ModelProdHealthFact(
        health=state, probed_at=_EVALUATED_AT, source="https://prod/health"
    )


def _command_missing_grant() -> ModelProdPromotionGateCommand:
    """Prod command whose promotion_grant is explicitly absent (fail closed)."""
    cmd = _command(projection=_ready_projection())
    return cmd.model_copy(update={"promotion_grant": None})


def _command_health_no_grant(
    state: EnumProdHealth,
) -> ModelProdPromotionGateCommand:
    """Prod command carrying a resolved prod_health fact but NO grant.

    The gate COMPUTE node does not consult prod_health (the health-conditional
    waiver lives in the resolver EFFECT / orchestrator), so this still fails
    closed on the missing grant regardless of the health value.
    """
    cmd = _command(projection=_ready_projection(), prod_health=_health(state))
    return cmd.model_copy(update={"promotion_grant": None})


# Each case: (id, command builder, expected allowed, expected reason substring,
#             expected image_digest-is-not-None).
_CASES: list[
    tuple[str, Callable[[], ModelProdPromotionGateCommand], bool, str, bool]
] = [
    # --- ALLOW paths ---
    (
        "dev-lane-noop-allow",
        lambda: _command(
            runtime_lane=EnumRuntimeLane.DEV, projection=None, rollback_target=None
        ),
        True,
        "not gated",
        True,
    ),
    (
        "stability-lane-noop-allow",
        lambda: _command(
            runtime_lane=EnumRuntimeLane.STABILITY_TEST,
            projection=None,
            rollback_target=None,
        ),
        True,
        "not gated",
        True,
    ),
    (
        "prod-full-valid-merged-allow",
        lambda: _command(
            projection=_ready_projection(), occ_state=EnumOccGateState.MERGED
        ),
        True,
        "prod reuses the stability digest",
        True,
    ),
    (
        "prod-full-valid-receipt-pass-allow",
        lambda: _command(
            projection=_ready_projection(),
            occ_state=EnumOccGateState.RECEIPT_GATE_PASS,
        ),
        True,
        "prod reuses the stability digest",
        True,
    ),
    (
        "prod-candidate-authorized-allow",
        lambda: _command(
            projection=_ready_projection(),
            grant=_grant(authorizes_candidate=True),
            promotion_class=EnumPromotionClass.STABILITY_CANDIDATE,
            non_main_lineage=True,
        ),
        True,
        "prod reuses the stability digest",
        True,
    ),
    # --- BLOCK paths (negative controls) ---
    (
        "prod-no-projection-fail-closed",
        lambda: _command(projection=None),
        False,
        "no stability-test readiness",
        False,
    ),
    (
        "prod-not-ready-block",
        lambda: _command(projection=_ready_projection(state="BLOCKED")),
        False,
        "not ready",
        False,
    ),
    (
        "prod-digest-drift-block",
        lambda: _command(
            requested_digest=_DIGEST_PROD_DRIFT, projection=_ready_projection()
        ),
        False,
        "does not match",
        False,
    ),
    (
        "prod-batch-mismatch-block",
        lambda: _command(projection=_ready_projection(batch="other-batch")),
        False,
        "promotion batch",
        False,
    ),
    (
        "prod-occ-pending-block",
        lambda: _command(
            projection=_ready_projection(), occ_state=EnumOccGateState.PENDING
        ),
        False,
        "occ",
        False,
    ),
    (
        "prod-missing-rollback-block",
        lambda: _command(projection=_ready_projection(), rollback_target=None),
        False,
        "rollback target",
        False,
    ),
    (
        "prod-missing-grant-block",
        _command_missing_grant,
        False,
        EnumProdGrantReason.MISSING_PROMOTION_GRANT.value,
        False,
    ),
    (
        "prod-self-granted-block",
        lambda: _command(
            projection=_ready_projection(), grant=_grant(approved_by=_REQUESTER)
        ),
        False,
        EnumProdGrantReason.SELF_GRANTED.value,
        False,
    ),
    (
        "prod-expired-grant-block",
        lambda: _command(
            projection=_ready_projection(),
            grant=_grant(expires_delta=timedelta(hours=-1)),
        ),
        False,
        EnumProdGrantReason.EXPIRED_PROMOTION_GRANT.value,
        False,
    ),
    (
        "prod-grant-lane-mismatch-block",
        lambda: _command(
            projection=_ready_projection(),
            grant=_grant(approved_lane=EnumRuntimeLane.STABILITY_TEST),
        ),
        False,
        EnumProdGrantReason.GRANT_LANE_MISMATCH.value,
        False,
    ),
    (
        "prod-candidate-unauthorized-block",
        lambda: _command(
            projection=_ready_projection(),
            promotion_class=EnumPromotionClass.STABILITY_CANDIDATE,
        ),
        False,
        EnumProdGrantReason.CANDIDATE_NOT_AUTHORIZED.value,
        False,
    ),
    # --- health fail-closed (this COMPUTE node ignores prod_health) ---
    (
        "prod-health-unknown-no-grant-fail-closed",
        lambda: _command_health_no_grant(EnumProdHealth.UNKNOWN),
        False,
        EnumProdGrantReason.MISSING_PROMOTION_GRANT.value,
        False,
    ),
    (
        "prod-health-unhealthy-no-grant-fail-closed",
        lambda: _command_health_no_grant(EnumProdHealth.UNHEALTHY),
        False,
        EnumProdGrantReason.MISSING_PROMOTION_GRANT.value,
        False,
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("builder", "expected_allowed", "reason_substr", "digest_present"),
    [(c[1], c[2], c[3], c[4]) for c in _CASES],
    ids=[c[0] for c in _CASES],
)
async def test_prod_promotion_gate_multiparam(
    builder: Callable[[], ModelProdPromotionGateCommand],
    expected_allowed: bool,
    reason_substr: str,
    digest_present: bool,
) -> None:
    handler = HandlerProdPromotionGate()
    command = builder()
    envelope: ModelEventEnvelope[ModelProdPromotionGateCommand] = ModelEventEnvelope(
        payload=command,
        correlation_id=command.correlation_id,
        event_type="onex.cmd.omnimarket.prod-promotion-gate-evaluate.v1",
    )

    output = await handler.handle(envelope)

    # Typed dispatch envelope assertions.
    assert output.node_kind == EnumNodeKind.COMPUTE
    assert output.correlation_id == command.correlation_id
    # COMPUTE purity: a gate is a pure decision, never an effect.
    assert output.events == ()
    assert output.intents == ()
    assert output.projections == ()

    decision = output.result
    assert isinstance(decision, ModelProdPromotionGateDecision)
    assert decision.allowed is expected_allowed
    assert reason_substr.lower() in decision.reason.lower()
    if expected_allowed and command.runtime_lane is EnumRuntimeLane.PROD:
        # An allowed prod promotion reuses the exact stability READY digest.
        assert decision.image_digest == _DIGEST_STABILITY
    if not expected_allowed:
        # A blocked prod promotion never hands a digest forward to the deploy.
        assert decision.image_digest is None


def test_block_and_allow_partition_is_nontrivial() -> None:
    """Guard: the matrix has both ALLOW and BLOCK cases (not all-pass / all-fail)."""
    allows = [c for c in _CASES if c[2] is True]
    blocks = [c for c in _CASES if c[2] is False]
    assert len(allows) >= 3, "need multiple distinct ALLOW param sets"
    assert len(blocks) >= 3, "need multiple distinct BLOCK negative controls"
