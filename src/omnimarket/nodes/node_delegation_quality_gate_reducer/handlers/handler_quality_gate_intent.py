# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerQualityGateIntent — executes ModelQualityGateIntent from the orchestrator.

Subscribes to onex.cmd.omnibase-infra.delegation-quality-gate-request.v1.
Receives ModelQualityGateIntent (wrapping a ModelQualityGateInput), runs the
deterministic quality gate reducer delta(), and publishes ModelQualityGateResult
to onex.evt.omnibase-infra.quality-gate-result.v1 so the orchestrator's
DispatcherQualityGateResult can consume it.

This handler is the Kafka-native quality-gate-intent consumer for the delegation
chain — the orchestrator publishes the intent, this node consumes it (OMN-12294).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from omnibase_compat.contracts.delegation.wire import ModelQualityGateIntent

from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

# Topic is sourced from the node contract at import time — never hardcoded inline.
_QUALITY_GATE_RESULT_TOPIC_SUFFIX = (
    "quality-gate-result.v1"  # onex-topic-allow: suffix used only for contract lookup
)


def _get_quality_gate_result_topic() -> str:
    """Return the full quality-gate-result publish topic from the contract.

    Fails fast at import time if the contract no longer declares the topic,
    preventing silent mis-wiring.
    """
    declared = contract_publish_topics(_CONTRACT_PATH)
    for topic in declared:
        if topic.endswith(_QUALITY_GATE_RESULT_TOPIC_SUFFIX):
            return topic
    raise RuntimeError(
        f"Contract {_CONTRACT_PATH} does not declare a publish topic ending with "
        f"{_QUALITY_GATE_RESULT_TOPIC_SUFFIX!r}. "
        "Update the contract before using HandlerQualityGateIntent."
    )


TOPIC_QUALITY_GATE_RESULT: str = _get_quality_gate_result_topic()


class HandlerQualityGateIntent:
    """Execute ModelQualityGateIntent and publish ModelQualityGateResult.

    Unwraps the orchestrator's quality-gate intent, runs the pure quality gate
    reducer delta() over the gate input, then publishes the result to the
    orchestrator's quality-gate-result subscribe topic.

    event_publisher is injected by the runtime dispatch machinery; when absent
    (unit tests) the result is returned but not published.
    """

    def __call__(
        self,
        intent: ModelQualityGateIntent,
        *,
        event_publisher: Any = None,
    ) -> ModelQualityGateResult:
        result = quality_gate_delta(intent.payload)
        logger.info(
            "HandlerQualityGateIntent resolved: passed=%s score=%.3f correlation_id=%s",
            result.passed,
            result.quality_score,
            result.correlation_id,
        )
        self._publish(result, event_publisher)
        return result

    def _publish(
        self,
        result: ModelQualityGateResult,
        event_publisher: Any,
    ) -> None:
        if event_publisher is None:
            return
        event_publisher.publish(TOPIC_QUALITY_GATE_RESULT, result)


__all__ = ["HandlerQualityGateIntent"]
