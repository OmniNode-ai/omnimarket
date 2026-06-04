# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-12294 reason="routing intent handler test references exact local HF model IDs to verify routing decisions"
"""Unit tests for HandlerRoutingIntent (OMN-12294).

Verifies the contract-native routing handler that replaces the in-process
DelegationIntentBridge.handle_routing_intent() path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    ModelDelegationRequest,
    ModelRoutingIntent,
)

from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    TOPIC_ROUTING_DECISION,
    HandlerRoutingIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
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


@pytest.mark.unit
class TestHandlerRoutingIntent:
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

    def _intent(self, **kwargs: object) -> ModelRoutingIntent:
        # The contract declares event_model=ModelRoutingIntent, so the runtime
        # validates the payload and hands handle() the typed intent.
        request = ModelDelegationRequest(
            prompt="Explain the runtime ingress boundary.",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=4096,
            emitted_at=datetime.now(UTC),
        )
        return ModelRoutingIntent(payload=request, **kwargs)  # type: ignore[arg-type]

    def test_unwraps_intent_and_returns_decision(self) -> None:
        handler = HandlerRoutingIntent()
        intent = self._intent()

        decision = handler.handle(intent)

        assert isinstance(decision, ModelRoutingDecision)
        assert decision.correlation_id == intent.payload.correlation_id
        assert decision.task_type == "research"
        assert decision.selected_model in {
            "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit",
            "Corianas/DeepSeek-R1-Distill-Qwen-14B-AWQ",
        }

    def test_returns_decision_for_runtime_autopublish(self) -> None:
        # The runtime dispatch-result applier publishes the RETURNED model to the
        # contract's published_events topic (routing-decision.v1).
        handler = HandlerRoutingIntent()
        decision = handler.handle(self._intent())
        assert isinstance(decision, ModelRoutingDecision)
        assert TOPIC_ROUTING_DECISION.endswith("routing-decision.v1")

    def test_min_tier_name_threaded_into_delta(self) -> None:
        handler = HandlerRoutingIntent()
        # min_tier_name=claude skips local/cheap_cloud; the test contract has only
        # local backends, so escalation to a higher floor yields no eligible tier
        # and the pure reducer raises — proving the field reaches delta().
        from omnibase_infra.errors import ProtocolConfigurationError

        with pytest.raises(ProtocolConfigurationError):
            handler.handle(self._intent(min_tier_name="claude"))

    def test_contract_declares_routing_decision_publish_topic(self) -> None:
        from omnimarket.nodes.contract_topics import contract_publish_topics

        contract = (
            Path(__file__).parents[3]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_delegation_routing_reducer"
            / "contract.yaml"
        )
        topics = contract_publish_topics(contract)
        assert any(t.endswith("routing-decision.v1") for t in topics)
