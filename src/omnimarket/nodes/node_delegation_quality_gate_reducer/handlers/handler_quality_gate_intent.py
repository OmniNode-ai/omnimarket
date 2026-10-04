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
from uuid import uuid4

from omnibase_core.models.delegation.wire import ModelQualityGateIntent
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_response_contract,
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
    """Execute ModelQualityGateIntent and return ModelQualityGateResult.

    Unwraps the orchestrator's quality-gate intent and runs the pure quality gate
    reducer delta() over the gate input. The returned ModelQualityGateResult is
    published to TOPIC_QUALITY_GATE_RESULT by the runtime dispatch-result applier
    (the contract's publish_topics drives the auto-publish) — the handler does
    not publish directly.

    ``handle`` is the runtime dispatch entrypoint (handler_wiring resolves
    handle/handle_async, never __call__).

    Both entrypoints use only the deterministic reducer (OMN-20164).
    """

    def handle(self, intent: ModelQualityGateIntent) -> ModelQualityGateResult:
        # OMN-15539: caller-declared structural requirements are authoritative;
        # resolve the task-class contract only when the request did not supply one.
        response_contract = (
            intent.payload.response_contract
            if intent.payload.response_contract is not None
            else resolve_task_class_response_contract(intent.payload.task_type)
        )
        result = quality_gate_delta(intent.payload, response_contract=response_contract)
        logger.info(
            "HandlerQualityGateIntent resolved: passed=%s score=%.3f "
            "score_verified=%s unverified_because=%s correlation_id=%s",
            result.passed,
            result.quality_score,
            result.score_verified,
            ",".join(result.score_unverified_because) or "-",
            result.correlation_id,
        )
        return result

    async def handle_async(
        self, intent: ModelQualityGateIntent
    ) -> ModelHandlerOutput[None]:
        """Run the deterministic gate and publish its result."""
        result = self.handle(intent)
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=intent.payload.correlation_id,
            handler_id="node_delegation_quality_gate_reducer.quality_gate_intent",
            events=(result,),
        )


__all__ = ["HandlerQualityGateIntent"]
