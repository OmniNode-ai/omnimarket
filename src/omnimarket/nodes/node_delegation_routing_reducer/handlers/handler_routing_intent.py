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
from typing import Any

from omnibase_compat.contracts.delegation.wire import ModelRoutingIntent

from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    delta as routing_delta,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
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
    """Execute ModelRoutingIntent and publish ModelRoutingDecision.

    Unwraps the orchestrator's routing intent, runs the pure routing reducer
    delta() with the delegation request and optional escalation tier floor,
    then publishes the decision to the orchestrator's routing-decision
    subscribe topic.

    event_publisher is injected by the runtime dispatch machinery; when absent
    (unit tests) the decision is returned but not published.
    """

    def __call__(
        self,
        intent: ModelRoutingIntent,
        *,
        event_publisher: Any = None,
    ) -> ModelRoutingDecision:
        decision = routing_delta(intent.payload, min_tier_name=intent.min_tier_name)
        logger.info(
            "HandlerRoutingIntent resolved: model=%s endpoint=%s tier=%s correlation_id=%s",
            decision.selected_model,
            decision.endpoint_url,
            decision.tier_name,
            decision.correlation_id,
        )
        self._publish(decision, event_publisher)
        return decision

    def _publish(
        self,
        decision: ModelRoutingDecision,
        event_publisher: Any,
    ) -> None:
        if event_publisher is None:
            return
        event_publisher.publish(TOPIC_ROUTING_DECISION, decision)


__all__ = ["HandlerRoutingIntent"]
