# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation intent bridge — executes intents emitted by the orchestrator.

The delegation orchestrator's dispatchers return intents as output_events:
  - ModelRoutingIntent -> needs routing reducer delta()
  - ModelInferenceIntent -> needs LLM call
  - ModelQualityGateIntent -> needs quality gate reducer delta()

Without this bridge, those intents are published as raw events with no
consumer on the other side. The bridge subscribes to the event bus and
executes each intent by calling the appropriate reducer or effect, then
publishes the result back to the topic that the orchestrator subscribes to.

This bridges the gap between the orchestrator's intent-based output and
the event-driven wiring, completing the delegation chain end-to-end.

Related:
    - OMN-7040: Node-based delegation pipeline
    - OMN-7381: Wire handler_build_dispatch to delegation orchestrator
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from pydantic import BaseModel

if TYPE_CHECKING:
    from omnibase_infra.protocols import ProtocolEventBusLike

from omnibase_infra.event_bus.topic_constants import (
    TOPIC_DELEGATION_INFERENCE_RESPONSE,
    TOPIC_DELEGATION_QUALITY_GATE_RESULT,
    TOPIC_DELEGATION_ROUTING_DECISION,
)

from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_quality_gate_intent import (
    ModelQualityGateIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    delta as routing_delta,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

logger = logging.getLogger(__name__)


class ProtocolLlmCaller(Protocol):
    """Protocol for calling LLM inference."""

    async def call(
        self, intent: ModelInferenceIntent
    ) -> ModelInferenceResponseData: ...


class DelegationIntentBridge:
    """Bridges delegation intents to reducers/effects and publishes results.

    This class completes the delegation chain by:
    1. Receiving intents emitted by the orchestrator dispatchers
    2. Executing the appropriate reducer (routing, quality gate) or effect (LLM)
    3. Publishing results back to topics the orchestrator subscribes to

    The bridge can operate in two modes:
    - **Direct mode**: Called directly with intent objects (for testing)
    - **Event bus mode**: Wired as a subscriber on intent topics

    Args:
        event_bus: Event bus for publishing results back to the orchestrator.
        llm_caller: Callable for LLM inference. If None, inference intents
            will raise an error.
    """

    def __init__(
        self,
        event_bus: ProtocolEventBusLike,
        llm_caller: ProtocolLlmCaller | None = None,
    ) -> None:
        self._event_bus: ProtocolEventBusLike = event_bus
        self._llm_caller = llm_caller
        self._inference_tasks: dict[
            tuple[UUID, str, str], asyncio.Task[ModelInferenceResponseData]
        ] = {}

    @property
    def llm_caller(self) -> ProtocolLlmCaller | None:
        """The LLM caller wired into this bridge, or None if not configured."""
        return self._llm_caller

    async def handle_routing_intent(
        self, intent: ModelRoutingIntent
    ) -> ModelRoutingDecision:
        """Execute routing reducer and publish decision.

        Calls the routing reducer delta() with the delegation request payload,
        then publishes the resulting ModelRoutingDecision to the routing-decision
        topic.
        """
        decision = routing_delta(intent.payload)
        logger.info(
            "Routing intent resolved: model=%s, endpoint=%s, correlation_id=%s",
            decision.selected_model,
            decision.endpoint_url,
            decision.correlation_id,
        )
        await self._publish(decision, TOPIC_DELEGATION_ROUTING_DECISION)
        return decision

    async def handle_inference_intent(
        self, intent: ModelInferenceIntent
    ) -> ModelInferenceResponseData:
        """Execute LLM inference and publish response.

        Calls the LLM caller with the inference intent, then publishes
        the response data to the inference-response topic.
        """
        key = (intent.correlation_id, intent.model, intent.prompt)
        task = self._inference_tasks.get(key)
        if task is not None:
            logger.info(
                "Inference intent already in flight; reusing result: "
                "model=%s correlation_id=%s",
                intent.model,
                intent.correlation_id,
            )
            return await task

        task = asyncio.create_task(self._execute_inference_intent(intent))
        self._inference_tasks[key] = task
        return await task

    async def _execute_inference_intent(
        self, intent: ModelInferenceIntent
    ) -> ModelInferenceResponseData:
        if self._llm_caller is None:
            msg = "No LLM caller configured — cannot execute inference intent"
            raise RuntimeError(msg)
        response = await self._llm_caller.call(intent)
        logger.info(
            "Inference intent resolved: model=%s, tokens=%d, correlation_id=%s",
            response.model_used,
            response.total_tokens,
            response.correlation_id,
        )
        await self._publish(response, TOPIC_DELEGATION_INFERENCE_RESPONSE)
        return response

    async def handle_quality_gate_intent(
        self, intent: ModelQualityGateIntent
    ) -> ModelQualityGateResult:
        """Execute quality gate reducer and publish result.

        Calls the quality gate reducer delta() with the gate input payload,
        then publishes the resulting ModelQualityGateResult to the
        quality-gate-result topic.
        """
        result = quality_gate_delta(intent.payload)
        logger.info(
            "Quality gate intent resolved: passed=%s, score=%.3f, correlation_id=%s",
            result.passed,
            result.quality_score,
            result.correlation_id,
        )
        await self._publish(result, TOPIC_DELEGATION_QUALITY_GATE_RESULT)
        return result

    async def handle_output_event(self, event: BaseModel) -> BaseModel | None:
        """Route an output event from a dispatcher to the appropriate handler.

        This is the main entry point when wired into the DispatchResultApplier
        or called from tests. It inspects the event type and dispatches to the
        correct handler method.

        Returns the result model, or None if the event type is not handled.
        """
        if isinstance(event, ModelRoutingIntent):
            return await self.handle_routing_intent(event)
        if isinstance(event, ModelInferenceIntent):
            return await self.handle_inference_intent(event)
        if isinstance(event, ModelQualityGateIntent):
            return await self.handle_quality_gate_intent(event)
        return None

    async def _publish(self, model: BaseModel, topic: str) -> None:
        """Publish a Pydantic model as an event envelope to the bus."""
        from datetime import UTC, datetime

        from omnibase_core.models.events.model_event_envelope import (
            ModelEventEnvelope,
        )

        correlation_id: UUID | None = getattr(model, "correlation_id", None)
        envelope: ModelEventEnvelope[BaseModel] = ModelEventEnvelope(
            payload=model,
            correlation_id=correlation_id,
            envelope_timestamp=datetime.now(UTC),
        )
        await self._event_bus.publish_envelope(envelope=envelope, topic=topic)


class MockLlmCaller:
    """Mock LLM caller for testing — returns synthetic responses."""

    def __init__(self, response_content: str = "") -> None:
        self._response_content = response_content or (
            "def test_example():\n"
            '    """Test example function.\n'
            "    Args:\n"
            "        None\n"
            "    Returns:\n"
            "        None\n"
            '    """\n'
            "    @pytest.mark.unit\n"
            "    result = example()\n"
            "    assert result is not None\n"
        )

    async def call(self, intent: ModelInferenceIntent) -> ModelInferenceResponseData:
        return ModelInferenceResponseData(
            correlation_id=intent.correlation_id,
            content=self._response_content,
            model_used=intent.model,
            latency_ms=42,
            prompt_tokens=100,
            completion_tokens=200,
            total_tokens=300,
        )


__all__: list[str] = [
    "DelegationIntentBridge",
    "MockLlmCaller",
    "ProtocolLlmCaller",
]
