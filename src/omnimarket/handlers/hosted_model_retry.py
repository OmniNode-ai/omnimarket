# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Advance a hosted customer's model list without reselecting their key."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from omnibase_core.models.delegation.wire import ModelRoutingIntent

from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    TENANT_OVERLAY_TIER_NAME,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
        DelegationWorkflowState,
    )


def next_customer_retry(
    workflow: DelegationWorkflowState,
    *,
    max_retries_for: Callable[[str], int | None],
) -> ModelRoutingDecision | ModelRoutingIntent | None:
    """Walk chosen models first; retain the bounded same-model retry afterward.

    An old decision with no candidates retains its original routing-intent
    behavior. A new decision always reuses its captured route/key, including
    a same-model reissue: a catalogue refresh must not restart or widen it.
    """
    decision = workflow.routing_decision
    if (
        workflow.current_tier_name != TENANT_OVERLAY_TIER_NAME
        or workflow.request is None
        or decision is None
    ):
        return None
    backend_ref = (decision.selected_backend_ref or "").strip()
    max_retries = max_retries_for(backend_ref)
    if max_retries is None:
        return None
    candidates = decision.model_candidates
    next_model = None
    if candidates:
        if decision.selected_model not in candidates:
            return None
        index = candidates.index(decision.selected_model) + 1
        if index < len(candidates):
            next_model = candidates[index]
    if workflow.customer_route_retry_backend_ref != backend_ref:
        workflow.customer_route_retry_backend_ref = backend_ref
        workflow.customer_route_retry_count = 0
    if next_model is None:
        if workflow.customer_route_retry_count >= max_retries:
            return None
        workflow.customer_route_retry_count += 1
    if candidates:
        return decision.model_copy(
            update={"selected_model": next_model or decision.selected_model}
        )
    return ModelRoutingIntent(payload=workflow.request)
