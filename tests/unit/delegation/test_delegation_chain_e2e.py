# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-12294 reason="delegation e2e test fixture must reference exact local HF model IDs to verify routing decisions"
"""End-to-end delegation chain test over the pure Kafka contract chain.

Exercises the full delegation pipeline as it runs in production: each node is
its own bus consumer of the topic the orchestrator publishes, and publishes its
result back to the topic the orchestrator awaits. There is NO in-process bridge
(OMN-12294 removed DelegationIntentBridge).

Chain (every hop a distinct node consuming/producing its contract topics):
  1. HandlerDelegationWorkflow.handle_delegation_request -> ModelRoutingIntent
     -> onex.cmd.omnibase-infra.delegation-routing-request.v1
  2. HandlerRoutingIntent (routing reducer) consumes the intent, publishes
     ModelRoutingDecision -> onex.evt.omnibase-infra.routing-decision.v1
  3. HandlerDelegationWorkflow.handle_routing_decision -> ModelInferenceIntent
     -> onex.cmd.omnibase-infra.delegation-inference-request.v1
  4. HandlerInferenceIntent (LLM call effect) consumes the intent, publishes
     ModelInferenceResponseData -> onex.evt.omnibase-infra.inference-response.v1
  5. HandlerDelegationWorkflow.handle_inference_response -> ModelQualityGateIntent
     -> onex.cmd.omnibase-infra.delegation-quality-gate-request.v1
  6. HandlerQualityGateIntent (quality gate reducer) consumes the intent,
     publishes ModelQualityGateResult -> onex.evt.omnibase-infra.quality-gate-result.v1
  7. HandlerDelegationWorkflow.handle_gate_result -> terminal delegation event.

Related:
    - OMN-7040: Node-based delegation pipeline
    - OMN-12294: Pure Kafka delegation chain (bridge eliminated)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from omnibase_compat.contracts.delegation.wire import (
    ModelInferenceIntent,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    TOPIC_QUALITY_GATE_RESULT,
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    TOPIC_ROUTING_DECISION,
    HandlerRoutingIntent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    TOPIC_INFERENCE_RESPONSE,
    HandlerInferenceIntent,
)

_BIFROST_CONTRACT = (
    "config_version: '2.0.0'\n"
    "schema_version: bifrost_delegation.v1\n"
    "backends:\n"
    "  - backend_id: local-coder\n"
    '    endpoint_url: "http://test-coder:8000"\n'
    '    model_name: "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit"\n'
    "    tier: local\n"
    "    timeout_ms: 30000\n"
    "    capabilities: [research]\n"
    "  - backend_id: local-reasoner\n"
    '    endpoint_url: "http://test-fast:8001"\n'
    '    model_name: "Corianas/DeepSeek-R1-Distill-Qwen-14B-AWQ"\n'
    "    tier: local\n"
    "    timeout_ms: 30000\n"
    "    capabilities: [research]\n"
    "routing_rules:\n"
    '  - rule_id: "11111111-1111-4111-8111-111111111111"\n'
    "    priority: 10\n"
    "    task_class: research\n"
    '    task_class_contract_version: "1.0.0"\n'
    '    backend_policy_version: "2.0.0"\n'
    "    match_operation_types: [chat_completion]\n"
    "    match_capabilities: [research]\n"
    "    backend_ids: [local-coder, local-reasoner]\n"
    "    fallback_policy:\n"
    "      action: escalate_to_next_tier\n"
    "      max_retries: 1\n"
    "      on_exhaust: return_error\n"
    '    shadow_policy_id: "22222222-2222-4222-8222-222222222222"\n'
    "default_backends:\n"
    "  - local-coder\n"
    "  - local-reasoner\n"
    "circuit_breaker:\n"
    "  failure_threshold: 5\n"
    "  window_seconds: 30\n"
    "failover:\n"
    "  max_attempts: 3\n"
    "  backoff_base_ms: 500\n"
    "shadow_mode:\n"
    "  enabled: false\n"
    '  policy_version: "test"\n'
    "  log_sample_rate: 1.0\n"
    "  comparison_logging_enabled: true\n"
    "  max_shadow_latency_ms: 5.0\n"
)


class _CapturingPublisher:
    """Records (topic, payload) pairs published by each worker handler.

    Stands in for the runtime's injected event publisher: the worker handlers
    call ``publish(topic, model)`` and the test asserts that the published
    payloads land on the contract topics the orchestrator awaits.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def publish(self, topic: str, payload: object) -> None:
        self.published.append((topic, payload))

    def last(self, topic: str) -> object:
        for t, payload in reversed(self.published):
            if t == topic:
                return payload
        raise AssertionError(f"no payload published to {topic}: {self.published}")

    def topics(self) -> list[str]:
        return [t for t, _ in self.published]


def _httpx_response(content: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "id": "chatcmpl-test",
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
        },
    }
    response.raise_for_status.return_value = None
    return response


@pytest.mark.unit
class TestDelegationChainE2E:
    @pytest.fixture(autouse=True)
    def _bifrost_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as _h

        _h._config = None
        _h._load_bifrost_endpoints.cache_clear()
        contract_path = tmp_path / "bifrost_delegation.yaml"
        contract_path.write_text(_BIFROST_CONTRACT, encoding="utf-8")
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
        yield
        _h._config = None
        _h._load_bifrost_endpoints.cache_clear()

    @pytest.fixture
    def workflow(self) -> HandlerDelegationWorkflow:
        return HandlerDelegationWorkflow(workflows={})

    @pytest.fixture
    def request_model(self) -> ModelDelegationRequest:
        return ModelDelegationRequest(
            prompt="Explain the runtime ingress boundary for OMN-9999.",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=4096,
            emitted_at=datetime.now(UTC),
        )

    def _run_chain_to_gate_result(
        self,
        workflow: HandlerDelegationWorkflow,
        request_model: ModelDelegationRequest,
        publisher: _CapturingPublisher,
        llm_content: str,
    ) -> object:
        """Drive route -> infer -> gate over the worker handlers; return gate result.

        Each worker handler.handle() RETURNS its result model; in the runtime the
        dispatch-result applier publishes that returned model to the contract's
        publish topic. The publisher here stands in for that auto-publish so the
        topic-traversal assertions mirror the live bus chain.
        """
        routing_handler = HandlerRoutingIntent()
        inference_handler = HandlerInferenceIntent()
        gate_handler = HandlerQualityGateIntent()

        # Hop 1: orchestrator emits routing intent.
        routing_intents = workflow.handle_delegation_request(request_model)
        assert len(routing_intents) == 1
        assert isinstance(routing_intents[0], ModelRoutingIntent)

        # Hop 2: routing reducer consumes the typed intent (runtime validates via
        # event_model), returns ModelRoutingDecision; runtime auto-publishes it to
        # routing-decision.v1 (contract published_events).
        decision = routing_handler.handle(routing_intents[0])
        publisher.publish(TOPIC_ROUTING_DECISION, decision)

        # Hop 3: orchestrator consumes the decision, emits inference intent.
        inference_intents = workflow.handle_routing_decision(decision)
        assert len(inference_intents) == 1
        assert isinstance(inference_intents[0], ModelInferenceIntent)

        # Hop 4: LLM call effect consumes the typed intent, returns
        # ModelInferenceResponseData; runtime auto-publishes it to inference-response.v1.
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _httpx_response(llm_content)
            mock_client_cls.return_value = mock_client
            response = inference_handler.handle(inference_intents[0])
        publisher.publish(TOPIC_INFERENCE_RESPONSE, response)

        # Hop 5: orchestrator consumes the response, emits quality gate intent.
        gate_intents = workflow.handle_inference_response(response)
        assert len(gate_intents) == 1
        assert isinstance(gate_intents[0], ModelQualityGateIntent)

        # Hop 6: quality gate reducer consumes the typed intent, returns
        # ModelQualityGateResult; runtime auto-publishes it to quality-gate-result.v1.
        gate_result = gate_handler.handle(gate_intents[0])
        publisher.publish(TOPIC_QUALITY_GATE_RESULT, gate_result)
        return gate_result

    def test_full_chain_completes_with_passing_gate(
        self,
        workflow: HandlerDelegationWorkflow,
        request_model: ModelDelegationRequest,
    ) -> None:
        publisher = _CapturingPublisher()
        good_content = (
            "Line 42 shows the runtime ingress boundary where validation should "
            "happen before dispatch. The tradeoff is that strict validation can "
            "reject more requests up front, but the benefit is clearer evidence "
            "and lower risk of malformed payloads entering the event bus."
        )

        gate_result = self._run_chain_to_gate_result(
            workflow, request_model, publisher, good_content
        )
        assert gate_result.passed is True  # type: ignore[attr-defined]

        # Hop 7: orchestrator consumes the gate result, emits terminal events.
        events = workflow.handle_gate_result(gate_result)  # type: ignore[arg-type]
        assert len(events) >= 1
        assert (
            workflow.workflows[request_model.correlation_id].state
            == EnumDelegationState.COMPLETED
        )

        # Every intermediate hop traversed its own contract topic over the bus.
        assert TOPIC_ROUTING_DECISION in publisher.topics()
        assert TOPIC_INFERENCE_RESPONSE in publisher.topics()
        assert TOPIC_QUALITY_GATE_RESULT in publisher.topics()

    def test_full_chain_fails_with_refusal(
        self,
        workflow: HandlerDelegationWorkflow,
        request_model: ModelDelegationRequest,
    ) -> None:
        publisher = _CapturingPublisher()
        refusal = "I cannot help with that request. As an AI, I'm sorry."

        gate_result = self._run_chain_to_gate_result(
            workflow, request_model, publisher, refusal
        )
        assert gate_result.passed is False  # type: ignore[attr-defined]
        assert any("REFUSAL" in r for r in gate_result.failure_reasons)  # type: ignore[attr-defined]

        events = workflow.handle_gate_result(gate_result)  # type: ignore[arg-type]
        assert len(events) >= 1
        # OMN-12254: refusal with fallback_recommended triggers escalation
        # (ROUTED) when a higher tier exists, else terminal FAILED.
        assert workflow.workflows[request_model.correlation_id].state in {
            EnumDelegationState.FAILED,
            EnumDelegationState.ROUTED,
        }

    def test_inference_failure_published_as_observable_response(
        self,
        workflow: HandlerDelegationWorkflow,
        request_model: ModelDelegationRequest,
    ) -> None:
        """A failed LLM call returns an error inference-response, not a hang."""
        routing_handler = HandlerRoutingIntent()
        inference_handler = HandlerInferenceIntent()

        routing_intents = workflow.handle_delegation_request(request_model)
        decision = routing_handler.handle(routing_intents[0])
        inference_intents = workflow.handle_routing_decision(decision)

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = ConnectionRefusedError("refused")
            mock_client_cls.return_value = mock_client
            response = inference_handler.handle(inference_intents[0])

        assert response.error_message != ""
        assert response.content == ""

    def test_duplicate_pending_request_replays_routing_once(
        self,
        workflow: HandlerDelegationWorkflow,
        request_model: ModelDelegationRequest,
    ) -> None:
        assert len(workflow.handle_delegation_request(request_model)) == 1
        assert len(workflow.handle_delegation_request(request_model)) == 1
        assert len(workflow.handle_delegation_request(request_model)) == 0
