# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure prod-promotion-gate COMPUTE handler (OMN-13211 / B3).

Re-expresses the ``node_redeploy`` ``_evaluate_prod_gate`` logic as a canonical
COMPUTE node. Pure: no I/O, no bus, no DB, no subprocess. It decides whether a
prod redeploy may proceed BEFORE any deploy effect — the first-class regression
guard against prod-vs-stability digest drift.

Rules (delegated to the shared gate functions):
  * non-prod lanes: always allowed (the gate is a no-op);
  * prod with a reducer-owned readiness projection: the full promotion gate
    (``evaluate_prod_promotion_gate``) — readiness READY for matching digest and
    batch, OCC evidence merged or Receipt-Gate-PASS, known rollback target;
  * prod without a readiness projection (legacy/un-gated request): fails closed
    via the same-digest gate with no stability readiness.

Dispatch:
  The runtime delivers a ``ModelEventEnvelope`` whose payload is a
  ``ModelProdPromotionGateCommand`` (or its dict form). The handler returns a
  ``ModelHandlerOutput.for_compute`` carrying the ``ModelProdPromotionGateDecision``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    EnumProdGrantReason,
    EnumPromotionClass,
    EnumRuntimeLane,
    ModelProdPromotionGateDecision,
    ModelProdPromotionInputs,
    evaluate_prod_digest_gate,
    evaluate_prod_promotion_gate,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.models.model_prod_promotion_gate_command import (
    ModelProdPromotionGateCommand,
)

HANDLER_ID = "prod-promotion-gate-compute"


def evaluate_gate(
    command: ModelProdPromotionGateCommand,
) -> ModelProdPromotionGateDecision:
    """Decide whether a redeploy may proceed for the command's lane.

    Pure function — the public, directly-testable surface. Non-prod lanes are
    allowed unconditionally; prod runs the full / same-digest promotion gate.
    """
    rollback_target = command.rollback_target or command.previous_image

    if command.runtime_lane is not EnumRuntimeLane.PROD:
        return ModelProdPromotionGateDecision(
            allowed=True,
            image_digest=command.requested_image_digest,
            rollback_target=rollback_target,
            reason=f"{command.runtime_lane.value} lane is not gated; deploy may proceed",
        )

    # OMN-13656: a stability-candidate / non-main-lineage image is refused for
    # prod BEFORE the no-projection same-digest fallback, so a workspace-built
    # candidate can never slip through the legacy un-gated path either. Authorized
    # only when an explicit candidate-authorizing grant is present.
    is_candidate = (
        command.promotion_class is EnumPromotionClass.STABILITY_CANDIDATE
        or command.non_main_lineage
    )
    candidate_authorized = (
        command.promotion_grant is not None
        and command.promotion_grant.authorizes_candidate
    )
    if is_candidate and not candidate_authorized:
        return ModelProdPromotionGateDecision(
            allowed=False,
            image_digest=None,
            rollback_target=rollback_target,
            reason=(
                f"{EnumProdGrantReason.CANDIDATE_NOT_AUTHORIZED.value}: image is a "
                "stability-candidate / non-main-lineage build "
                f"(promotion_class={command.promotion_class.value!r}, "
                f"non_main_lineage={command.non_main_lineage}); it is pinnable to "
                "dev/stability only and is refused for prod absent a grant that "
                "explicitly authorizes the candidate class"
            ),
        )

    if command.readiness_projection is None:
        digest_gate = evaluate_prod_digest_gate(
            requested_digest=command.requested_image_digest,
            stability_readiness=None,
        )
        return ModelProdPromotionGateDecision(
            allowed=digest_gate.allowed,
            image_digest=digest_gate.image_digest,
            rollback_target=rollback_target,
            reason=digest_gate.reason,
        )

    if command.evaluated_at is None:
        raise ValueError(
            "prod promotion gate requires a deterministic evaluated_at stamped by "
            "the orchestrator/runtime; the compute never calls datetime.now()"
        )

    return evaluate_prod_promotion_gate(
        ModelProdPromotionInputs(
            requested_image_digest=command.requested_image_digest,
            promotion_batch_id=command.promotion_batch_id,
            readiness_projection=command.readiness_projection,
            occ_gate_state=command.occ_gate_state,
            rollback_target=rollback_target,
            requested_by=command.requested_by,
            promotion_grant=command.promotion_grant,
            promotion_class=command.promotion_class,
            non_main_lineage=command.non_main_lineage,
            evaluated_at=command.evaluated_at,
        )
    )


class HandlerProdPromotionGate:
    """Canonical COMPUTE handler: prod promotion facts -> gate decision."""

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[ModelProdPromotionGateDecision]:
        """Pure compute: evaluate the prod gate for the request payload."""
        command = _coerce_command(envelope.payload)
        decision = evaluate_gate(command)
        return ModelHandlerOutput.for_compute(
            input_envelope_id=envelope.envelope_id,
            correlation_id=envelope.correlation_id or command.correlation_id or uuid4(),
            handler_id=HANDLER_ID,
            result=decision,
        )


def _coerce_command(payload: Any) -> ModelProdPromotionGateCommand:
    """Coerce the dispatched payload into a ``ModelProdPromotionGateCommand``."""
    if isinstance(payload, ModelProdPromotionGateCommand):
        return payload
    if isinstance(payload, Mapping):
        return ModelProdPromotionGateCommand.model_validate(dict(payload))
    if hasattr(payload, "model_dump"):
        return ModelProdPromotionGateCommand.model_validate(payload.model_dump())
    raise TypeError(
        f"prod promotion gate payload must be ModelProdPromotionGateCommand or a "
        f"mapping; got {type(payload).__name__}"
    )


__all__: list[str] = ["HANDLER_ID", "HandlerProdPromotionGate", "evaluate_gate"]
