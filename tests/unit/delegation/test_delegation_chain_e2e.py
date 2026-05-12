# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""End-to-end delegation chain test using EventBusInmemory.

Exercises the full delegation pipeline:
  1. Build dispatch creates a delegation request
  2. DispatcherDelegationRequest -> HandlerDelegationWorkflow -> emits ModelRoutingIntent
  3. DelegationIntentBridge -> routing delta() -> publishes ModelRoutingDecision
  4. DispatcherRoutingDecision -> HandlerDelegationWorkflow -> emits ModelInferenceIntent
  5. DelegationIntentBridge -> mock LLM -> publishes ModelInferenceResponseData
  6. (Inference response triggers) -> HandlerDelegationWorkflow -> emits ModelQualityGateIntent
  7. DelegationIntentBridge -> quality gate delta() -> publishes ModelQualityGateResult
  8. DispatcherQualityGateResult -> HandlerDelegationWorkflow -> emits delegation-completed

This test proves the chain completes end-to-end with the intent bridge wired.

Related:
    - OMN-7040: Node-based delegation pipeline
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.topic_constants import (
    TOPIC_DELEGATION_INFERENCE_RESPONSE,
    TOPIC_DELEGATION_QUALITY_GATE_RESULT,
    TOPIC_DELEGATION_ROUTING_DECISION,
)

from omnimarket.nodes.node_delegation_orchestrator.delegation_intent_bridge import (
    DelegationIntentBridge,
    MockLlmCaller,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import (
    EnumDelegationState,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
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
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)


@pytest.mark.unit
class TestDelegationChainE2E:
    """End-to-end delegation chain tests using in-memory event bus."""

    @pytest.fixture(autouse=True)
    def _setup_bifrost_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Set the bifrost contract path used by the routing reducer."""
        import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as _h

        _h._config = None
        _h._load_bifrost_endpoints.cache_clear()
        contract_path = tmp_path / "bifrost_delegation.yaml"
        contract_path.write_text(
            "config_version: '1.1.0'\n"
            "schema_version: bifrost_delegation.v1\n"
            "backends:\n"
            "  - backend_id: local-qwen-coder-30b\n"
            '    endpoint_url: "http://test-coder:8000"\n'
            '    model_name: "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit"\n'
            "    tier: local\n"
            "    timeout_ms: 30000\n"
            "    capabilities: []\n"
            "  - backend_id: local-deepseek-r1-14b\n"
            '    endpoint_url: "http://test-fast:8001"\n'
            '    model_name: "Corianas/DeepSeek-R1-Distill-Qwen-14B-AWQ"\n'
            "    tier: local\n"
            "    timeout_ms: 30000\n"
            "    capabilities: []\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
        yield
        _h._config = None
        _h._load_bifrost_endpoints.cache_clear()

    @pytest.fixture
    def handler(self) -> HandlerDelegationWorkflow:
        return HandlerDelegationWorkflow()

    @pytest.fixture
    def correlation_id(self) -> object:
        return uuid4()

    @pytest.fixture
    def delegation_request(self, correlation_id: object) -> ModelDelegationRequest:
        return ModelDelegationRequest(
            prompt="Run ticket-pipeline for OMN-9999",
            task_type="research",
            correlation_id=correlation_id,
            max_tokens=4096,
            emitted_at=datetime.now(UTC),
        )

    @pytest.mark.asyncio
    async def test_full_chain_completes_with_passing_gate(
        self,
        handler: HandlerDelegationWorkflow,
        delegation_request: ModelDelegationRequest,
    ) -> None:
        """Full chain: request -> route -> infer -> gate pass -> COMPLETED."""
        bus = EventBusInmemory(environment="test", group="delegation-test")
        await bus.start()

        # Good response content that passes quality gate for "research" task
        good_content = (
            "Based on my analysis of the codebase, the ticket OMN-9999 "
            "requires changes to the delegation pipeline. The routing "
            "reducer needs to handle the new task type. Here is a detailed "
            "breakdown of the changes needed across the affected modules."
        )
        bridge = DelegationIntentBridge(
            event_bus=bus,
            llm_caller=MockLlmCaller(response_content=good_content),
        )

        cid = delegation_request.correlation_id

        # Step 1: Handle delegation request -> get routing intents
        routing_intents = handler.handle_delegation_request(delegation_request)
        assert len(routing_intents) == 1
        assert isinstance(routing_intents[0], ModelRoutingIntent)

        # Step 2: Bridge executes routing intent -> routing decision
        decision = await bridge.handle_routing_intent(routing_intents[0])
        assert isinstance(decision, ModelRoutingDecision)
        assert decision.correlation_id == cid
        assert decision.task_type == "research"
        assert decision.selected_model in {
            "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit",
            "Corianas/DeepSeek-R1-Distill-Qwen-14B-AWQ",
        }

        # Step 3: Handle routing decision -> get inference intents
        inference_intents = handler.handle_routing_decision(decision)
        assert len(inference_intents) == 1
        assert isinstance(inference_intents[0], ModelInferenceIntent)
        assert inference_intents[0].correlation_id == cid

        # Step 4: Bridge executes inference intent -> inference response
        response = await bridge.handle_inference_intent(inference_intents[0])
        assert isinstance(response, ModelInferenceResponseData)
        assert response.correlation_id == cid
        assert response.content == good_content

        # Step 5: Handle inference response -> get quality gate intents
        gate_intents = handler.handle_inference_response(response)
        assert len(gate_intents) == 1
        assert isinstance(gate_intents[0], ModelQualityGateIntent)

        # Step 6: Bridge executes quality gate intent -> gate result
        gate_result = await bridge.handle_quality_gate_intent(gate_intents[0])
        assert isinstance(gate_result, ModelQualityGateResult)
        assert gate_result.correlation_id == cid
        assert gate_result.passed is True, (
            f"Quality gate should pass for good research content, "
            f"but got score={gate_result.quality_score}, "
            f"reasons={gate_result.failure_reasons}"
        )

        # Step 7: Handle gate result -> get completion events
        events = handler.handle_gate_result(gate_result)
        assert len(events) >= 1  # delegation-completed + compat event + baseline

        # Verify terminal state
        workflow = handler.workflows[cid]
        assert workflow.state == EnumDelegationState.COMPLETED

        # Verify events published to bus
        history = await bus.get_event_history(limit=100)
        topics_published = [msg.topic for msg in history]
        assert TOPIC_DELEGATION_ROUTING_DECISION in topics_published
        assert TOPIC_DELEGATION_INFERENCE_RESPONSE in topics_published
        assert TOPIC_DELEGATION_QUALITY_GATE_RESULT in topics_published

        await bus.close()

    @pytest.mark.asyncio
    async def test_full_chain_fails_with_refusal(
        self,
        handler: HandlerDelegationWorkflow,
        delegation_request: ModelDelegationRequest,
    ) -> None:
        """Full chain: request -> route -> infer (refusal) -> gate fail -> FAILED."""
        bus = EventBusInmemory(environment="test", group="delegation-test")
        await bus.start()

        # Bad response that triggers refusal detection
        refusal_content = "I cannot help with that request. As an AI, I'm sorry."
        bridge = DelegationIntentBridge(
            event_bus=bus,
            llm_caller=MockLlmCaller(response_content=refusal_content),
        )

        cid = delegation_request.correlation_id

        # Run through the chain
        routing_intents = handler.handle_delegation_request(delegation_request)
        decision = await bridge.handle_routing_intent(routing_intents[0])
        inference_intents = handler.handle_routing_decision(decision)
        response = await bridge.handle_inference_intent(inference_intents[0])
        gate_intents = handler.handle_inference_response(response)
        gate_result = await bridge.handle_quality_gate_intent(gate_intents[0])

        assert gate_result.passed is False
        assert any("REFUSAL" in r for r in gate_result.failure_reasons)

        events = handler.handle_gate_result(gate_result)
        assert len(events) >= 1

        workflow = handler.workflows[cid]
        assert workflow.state == EnumDelegationState.FAILED

        await bus.close()

    @pytest.mark.asyncio
    async def test_handle_output_event_routes_correctly(
        self,
        handler: HandlerDelegationWorkflow,
        delegation_request: ModelDelegationRequest,
    ) -> None:
        """Verify handle_output_event dispatches to correct handler."""
        bus = EventBusInmemory(environment="test", group="delegation-test")
        await bus.start()

        bridge = DelegationIntentBridge(
            event_bus=bus,
            llm_caller=MockLlmCaller(),
        )

        # Get a routing intent from the handler
        routing_intents = handler.handle_delegation_request(delegation_request)
        intent = routing_intents[0]

        # Use handle_output_event (the generic dispatcher)
        result = await bridge.handle_output_event(intent)
        assert isinstance(result, ModelRoutingDecision)

        await bus.close()

    @pytest.mark.asyncio
    async def test_bridge_publishes_to_correct_topics(
        self,
        handler: HandlerDelegationWorkflow,
        delegation_request: ModelDelegationRequest,
    ) -> None:
        """Verify bridge publishes results to the correct event bus topics."""
        bus = EventBusInmemory(environment="test", group="delegation-test")
        await bus.start()

        bridge = DelegationIntentBridge(
            event_bus=bus,
            llm_caller=MockLlmCaller(),
        )

        # Run routing intent
        routing_intents = handler.handle_delegation_request(delegation_request)
        await bridge.handle_routing_intent(routing_intents[0])

        # Check routing decision was published
        routing_history = await bus.get_event_history(
            topic=TOPIC_DELEGATION_ROUTING_DECISION
        )
        assert len(routing_history) == 1

        # Continue chain to get inference intent
        decision = ModelRoutingDecision.model_validate(
            _extract_payload(routing_history[0])
        )
        inference_intents = handler.handle_routing_decision(decision)
        await bridge.handle_inference_intent(inference_intents[0])

        # Check inference response was published
        inference_history = await bus.get_event_history(
            topic=TOPIC_DELEGATION_INFERENCE_RESPONSE
        )
        assert len(inference_history) == 1

        await bus.close()

    @pytest.mark.asyncio
    async def test_chain_idempotent_duplicate_request(
        self,
        handler: HandlerDelegationWorkflow,
        delegation_request: ModelDelegationRequest,
    ) -> None:
        """Duplicate delegation request returns empty (idempotent)."""
        bus = EventBusInmemory(environment="test", group="delegation-test")
        await bus.start()

        _bridge = DelegationIntentBridge(
            event_bus=bus,
            llm_caller=MockLlmCaller(),
        )
        del _bridge  # created to verify construction; not used in idempotency check

        # First request creates workflow
        intents1 = handler.handle_delegation_request(delegation_request)
        assert len(intents1) == 1

        # Duplicate request returns empty
        intents2 = handler.handle_delegation_request(delegation_request)
        assert len(intents2) == 0

        await bus.close()


def _extract_payload(message: object) -> dict[str, object]:
    """Extract payload dict from an EventBusInmemory message."""
    import json

    value = getattr(message, "value", b"{}")
    data = json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
    return data.get("payload", data)
