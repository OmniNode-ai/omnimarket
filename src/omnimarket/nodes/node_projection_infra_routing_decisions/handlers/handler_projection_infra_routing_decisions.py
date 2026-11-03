# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure fold over one routing-decision delivery.

Rule 7a definition-B: ``handle(request) -> result``. No clock, no broker, no
database, so the same delivery always folds to the same row and a replay is
byte-identical. The wire-name to column-name translation is the one the retired
``infra_routing_decisions`` consumer applied, restated here rather than imported
so this node outlives that module:

    selected_backend_ref -> selected_provider
    tier_name            -> selected_tier
    rationale            -> reason

A delivery the old consumer sent to its dead-letter queue is a poison event
here: no dict payload, a correlation id that is not a UUID, or no selected
model. Retrying any of them cannot succeed.

This class is half of the pair. The writer beside it is the entry the runtime
calls; a pure entry alone on a projection arm validates, returns and stores
nothing while every watermark reads healthy.
"""

from __future__ import annotations

from uuid import UUID

from omnimarket.nodes.node_projection_infra_routing_decisions.models import (
    ModelInfraRoutingDecisionProjectionRequest,
    ModelInfraRoutingDecisionProjectionResult,
    ModelInfraRoutingDecisionRow,
)
from omnimarket.projection.error_classification import PoisonEventError


class HandlerProjectionInfraRoutingDecisions:
    """Folds one routing-decision delivery into the row it becomes."""

    def handle(
        self, request: ModelInfraRoutingDecisionProjectionRequest
    ) -> ModelInfraRoutingDecisionProjectionResult:
        delivery = request.delivery
        # The envelope the reducer publishes carries the decision under
        # ``payload``; the runtime hands an in-process writer the decision itself.
        wrapped = delivery.get("payload")
        payload = wrapped if isinstance(wrapped, dict) else delivery
        if "payload" in delivery and not isinstance(wrapped, dict):
            raise PoisonEventError("routing-decision envelope has no dict payload")

        correlation_raw = payload.get("correlation_id") or delivery.get(
            "correlation_id"
        )
        correlation_id: UUID | None = None
        if correlation_raw is not None:
            try:
                correlation_id = UUID(str(correlation_raw))
            except ValueError as error:
                raise PoisonEventError(
                    "routing-decision correlation_id is not a UUID"
                ) from error

        selected_model = payload.get("selected_model")
        if not isinstance(selected_model, str) or not selected_model:
            raise PoisonEventError("routing-decision names no selected_model")

        task_type = payload.get("task_type")
        return ModelInfraRoutingDecisionProjectionResult(
            row=ModelInfraRoutingDecisionRow(
                correlation_id=correlation_id,
                selected_provider=_as_str(payload.get("selected_backend_ref")),
                selected_tier=_as_str(payload.get("tier_name")),
                selected_model=selected_model,
                reason=_as_str(payload.get("rationale")),
                task_type=task_type if isinstance(task_type, str) else None,
            )
        )


def _as_str(value: object) -> str:
    """An absent wire string and an empty one are the same NOT NULL '' fact."""
    return value if isinstance(value, str) else ""


__all__: list[str] = ["HandlerProjectionInfraRoutingDecisions"]
