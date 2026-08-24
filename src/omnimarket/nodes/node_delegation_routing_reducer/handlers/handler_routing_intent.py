# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRoutingIntent — executes ModelRoutingIntent from the delegation orchestrator.

Subscribes to onex.cmd.omnibase-infra.delegation-routing-request.v1.
Receives ModelRoutingIntent (the delegation request plus an optional
min_tier_name escalation hint), runs the deterministic routing reducer
delta(), and publishes ModelRoutingDecision to
onex.evt.omnibase-infra.routing-decision.v1 so the orchestrator's
DispatcherRoutingDecision can consume it.

This handler is the Kafka-native routing-intent consumer for the delegation
chain — the orchestrator publishes the intent, this node consumes it (OMN-12294).
"""

from __future__ import annotations

import logging
from pathlib import Path

from omnibase_core.models.delegation.wire import ModelRoutingIntent

from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    delta as routing_delta,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.routing.tenant_overlay_resolver import (
    ProtocolTenantOverlayReader,
    resolve_tenant_overlay,
    resolve_tenant_overlay_db,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

# Topic is sourced from the node contract at import time — never hardcoded inline.
_ROUTING_DECISION_TOPIC_SUFFIX = (
    "routing-decision.v1"  # onex-topic-allow: suffix used only for contract lookup
)


def _get_routing_decision_topic() -> str:
    """Return the full routing-decision publish topic from the contract.

    Fails fast at import time if the contract no longer declares the topic,
    preventing silent mis-wiring.
    """
    declared = contract_publish_topics(_CONTRACT_PATH)
    for topic in declared:
        if topic.endswith(_ROUTING_DECISION_TOPIC_SUFFIX):
            return topic
    raise RuntimeError(
        f"Contract {_CONTRACT_PATH} does not declare a publish topic ending with "
        f"{_ROUTING_DECISION_TOPIC_SUFFIX!r}. "
        "Update the contract before using HandlerRoutingIntent."
    )


TOPIC_ROUTING_DECISION: str = _get_routing_decision_topic()


class HandlerRoutingIntent:
    """Execute ModelRoutingIntent and return ModelRoutingDecision.

    Unwraps the orchestrator's routing intent and runs the pure routing reducer
    delta() with the delegation request and optional escalation tier floor. The
    returned ModelRoutingDecision is published to TOPIC_ROUTING_DECISION by the
    runtime dispatch-result applier (the contract's publish_topics drives the
    auto-publish) — the handler does not publish directly.

    ``handle`` is the runtime dispatch entrypoint (handler_wiring resolves
    handle/handle_async, never __call__).
    """

    def __init__(
        self,
        *,
        tenant_overlay_db: ProtocolTenantOverlayReader | None = None,
    ) -> None:
        # OMN-15631 v1(a): resolved lazily (once, at construction — not per
        # request) via resolve_tenant_overlay_db(), which is itself gated on
        # OMNIDASH_ANALYTICS_DB_URL and fails OPEN to None when unset/
        # unreachable — a request from a tenant with no overlay row, or from
        # a lane with no overlay DB wired at all, resolves the unchanged
        # platform default (see delta()'s tenant_overlay=None docstring).
        # Tests inject a fake reader here instead of setting the env var.
        self._tenant_overlay_db = (
            tenant_overlay_db
            if tenant_overlay_db is not None
            else resolve_tenant_overlay_db()
        )

    def handle(self, intent: ModelRoutingIntent) -> ModelRoutingDecision:
        # OMN-14402: getattr-guarded so this consumer degrades gracefully
        # against a core pin that predates the excluded_backend_refs field
        # (mirrors the OMN-14280 tenant_id rollout-skew pattern) instead of
        # raising AttributeError on a mixed-deploy window.
        excluded_backend_refs = frozenset(
            getattr(intent, "excluded_backend_refs", ()) or ()
        )
        # OMN-15631 v1(a): resolve the tenant overlay ONCE per request, as a
        # pure input threaded into delta() — mirrors the roi_overlay pattern.
        # getattr-guarded the same way excluded_backend_refs is above, for a
        # payload built against a core pin that predates ModelDelegationRequest
        # carrying tenant_id (there is none known today, but the pattern is
        # cheap insurance against the identical rollout-skew failure mode).
        tenant_id = getattr(intent.payload, "tenant_id", None)
        tenant_overlay = resolve_tenant_overlay(
            self._tenant_overlay_db,
            tenant_id=tenant_id,
            task_type=intent.payload.task_type,
        )
        decision = routing_delta(
            intent.payload,
            min_tier_name=intent.min_tier_name,
            excluded_backend_refs=excluded_backend_refs,
            tenant_overlay=tenant_overlay,
        )
        logger.info(
            "HandlerRoutingIntent resolved: model=%s endpoint=%s tier=%s correlation_id=%s",
            decision.selected_model,
            decision.endpoint_url,
            decision.tier_name,
            decision.correlation_id,
        )
        return decision


__all__ = ["HandlerRoutingIntent"]
