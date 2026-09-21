# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure fold: one gate decision becomes one row (OMN-18999).

No database, no bus, no clock. Everything this module decides is decided from
the delivered payload, so the row a live lane writes and the row a unit test
asserts are produced by the same code path.

THE ONE INFERENCE THIS MODULE MAKES, AND ITS LIMIT
    A decision minted before OMN-18999 carries no ``outcome``. For the
    authorization-grant branches the typed token is still recoverable: those
    decisions have always been built by ``_grant_blocked``, which writes
    ``"<token>: <detail>"``, so the token is the text before the first colon
    and it is READ rather than guessed. The inference is bounded by a
    membership test against the enum -- a sentence whose first word happens to
    end in a colon does not become an outcome.

    For the six readiness / digest / evidence refusals there is nothing to
    recover: those branches never carried a token, which is the defect the
    ticket is about. Those project to ``unknown``, and ``unknown`` is a real
    answer meaning "this decision predates the typed code", never a stand-in
    for a branch nobody asserted.
"""

from __future__ import annotations

from uuid import UUID

from omnimarket.events.runtime_deployment import EnumProdGateOutcome
from omnimarket.nodes.node_projection_prod_promotion_gate.models import (
    UNKNOWN_OUTCOME,
    ModelProdPromotionGateProjectionRequest,
    ModelProdPromotionGateProjectionResult,
    ModelProdPromotionGateRow,
)

HANDLER_ID = "projection-prod-promotion-gate"

_OUTCOME_VALUES: frozenset[str] = frozenset(
    member.value for member in EnumProdGateOutcome
)


def resolve_outcome(outcome: str | None, reason: str) -> str:
    """Resolve the typed branch code for one decision.

    The declared ``outcome`` wins whenever it is present and known. A value
    that is present but NOT a known member is deliberately not passed through:
    a token this build cannot interpret is recorded as unknown rather than
    written into a column every query then has to special-case.
    """
    if outcome is not None and outcome in _OUTCOME_VALUES:
        return outcome

    head, separator, _ = reason.partition(":")
    if separator and head.strip() in _OUTCOME_VALUES:
        return head.strip()

    return UNKNOWN_OUTCOME


class HandlerProjectionProdPromotionGate:
    """Folds one prod-promotion-gate decision into its durable row."""

    def handle(
        self, request: ModelProdPromotionGateProjectionRequest
    ) -> ModelProdPromotionGateProjectionResult:
        """Fold one decision. Pure."""
        event = request.event
        context = event.deploy_context

        correlation_id = event.correlation_id
        if correlation_id is None:
            # The decision carried no run identity. The delivery's own
            # deterministic identifier is derived from topic/partition/offset,
            # so a redelivery of the SAME message converges on the same row
            # rather than duplicating -- which is the property the key exists
            # for. It is a weaker identity than the run's own and is used only
            # where there is no run identity to use.
            correlation_id = UUID(request.fallback_correlation_id)

        row = ModelProdPromotionGateRow(
            correlation_id=correlation_id,
            outcome=resolve_outcome(event.outcome, event.reason),
            allowed=event.allowed,
            reason=event.reason,
            grant_id=event.grant_id,
            requested_image_digest=(
                event.requested_image_digest
                # A pre-OMN-18999 decision echoed no requested digest, but the
                # deploy context it already carried holds the one the request
                # named. Reading it is recovery of a fact that is present, not
                # a substitute for one that is absent.
                or (None if context is None else context.image_digest)
            ),
            resolved_image_digest=event.image_digest,
            rollback_target=event.rollback_target,
            runtime_lane=(None if context is None else context.runtime_lane.value),
            promotion_batch_id=(
                None if context is None else context.promotion_batch_id
            ),
            evaluated_at=event.evaluated_at,
            source_topic=request.source_topic,
        )
        return ModelProdPromotionGateProjectionResult(row=row)


__all__ = [
    "HANDLER_ID",
    "HandlerProjectionProdPromotionGate",
    "resolve_outcome",
]
