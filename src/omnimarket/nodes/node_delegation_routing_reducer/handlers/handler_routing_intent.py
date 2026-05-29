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

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        intent = ModelRoutingIntent(**input_data)
        decision = routing_delta(intent.payload, min_tier_name=intent.min_tier_name)
        logger.info(
            "HandlerRoutingIntent resolved: model=%s endpoint=%s tier=%s correlation_id=%s",
            decision.selected_model,
            decision.endpoint_url,
            decision.tier_name,
            decision.correlation_id,
        )
        return decision.model_dump(mode="json")


__all__ = ["HandlerRoutingIntent"]
