# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain checks that delegation returns useful artifacts, not just text."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_quality_gate_intent import (
    ModelQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)

_BIFROST_CONTRACT = """\
config_version: '2.0.0'
schema_version: bifrost_delegation.v1
backends:
  - backend_id: local-coder
    endpoint_url: "http://test-coder:8000"
    model_name: "test-local-coder"
    tier: local
    timeout_ms: 30000
    capabilities: [code_completion, context_window_large]
default_backends:
  - local-coder
routing_rules:
  - rule_id: "55555555-5555-4555-8555-555555555555"
    priority: 10
    task_class: test
    task_class_contract_version: "1.0.0"
    backend_policy_version: "2.0.0"
    match_operation_types: [chat_completion]
    match_capabilities: [code_completion]
    backend_ids: [local-coder]
    fallback_policy:
      action: return_error
      max_retries: 0
      on_exhaust: return_error
    shadow_policy_id: "66666666-6666-4666-8666-666666666666"
  - rule_id: "77777777-7777-4777-8777-777777777777"
    priority: 10
    task_class: code_generation
    task_class_contract_version: "1.0.0"
    backend_policy_version: "2.0.0"
    match_operation_types: [chat_completion]
    match_capabilities: [code_completion]
    backend_ids: [local-coder]
    fallback_policy:
      action: return_error
      max_retries: 0
      on_exhaust: return_error
    shadow_policy_id: "88888888-8888-4888-8888-888888888888"
circuit_breaker:
  failure_threshold: 5
  window_seconds: 30
failover:
  max_attempts: 1
  backoff_base_ms: 0
shadow_mode:
  enabled: false
  policy_version: "test"
  log_sample_rate: 1.0
  comparison_logging_enabled: true
  max_shadow_latency_ms: 5.0
"""

_TEST_ARTIFACT = """\
import pytest


@pytest.mark.unit
def test_normalize_unit_state_maps_running_to_healthy():
    assert normalize_unit_state(" running ") == "healthy"


@pytest.mark.unit
def test_normalize_unit_state_rejects_blank_input():
    # Edge case: blank input should raise an error.
    with pytest.raises(ValueError):
        normalize_unit_state("   ")
"""

_PREDICATE_TEST_ARTIFACT = """\
import pytest


@pytest.mark.unit
def test_normalize_status_valid_inputs():
    assert normalize_status("ok") is True
    assert normalize_status("OK") is True
    assert normalize_status("HeAlThY") is True
    assert normalize_status("pAsS") is True


@pytest.mark.unit
def test_normalize_status_invalid_inputs():
    assert normalize_status("unknown") is False
    assert normalize_status("") is False
    assert normalize_status(None) is False
"""

_CODE_GENERATION_ARTIFACT = """\
def normalize_status(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("blank input")
    lowered = stripped.lower()
    if lowered in {"ok", "success", "pass"}:
        return "ok"
    if lowered in {"failed", "error", "fail"}:
        return "failed"
    return lowered


def test_normalize_status_maps_success_values():
    # Typed helper keeps existing tests compatible with no regression.
    assert normalize_status(" SUCCESS ") == "ok"


def test_normalize_status_rejects_blank_input():
    import pytest

    with pytest.raises(ValueError):
        normalize_status("   ")
"""


@pytest.fixture(autouse=True)
def _bifrost_contract(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    handler_delegation_routing._config = None
    handler_delegation_routing._load_bifrost_endpoints.cache_clear()
    handler_delegation_routing._get_task_class_contract.cache_clear()
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_CONTRACT, encoding="utf-8")
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    yield
    handler_delegation_routing._config = None
    handler_delegation_routing._load_bifrost_endpoints.cache_clear()
    handler_delegation_routing._get_task_class_contract.cache_clear()


def _httpx_response(content: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "id": "chatcmpl-useful-artifact",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": content},
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
        },
    }
    response.raise_for_status.return_value = None
    return response


@pytest.mark.integration
@pytest.mark.parametrize(
    ("task_type", "prompt", "artifact", "expected_markers"),
    [
        (
            "test",
            (
                "Write pytest unit tests for omnibase_infra "
                "normalize_unit_state(state: str)."
            ),
            _TEST_ARTIFACT,
            ("@pytest.mark.unit", "with pytest.raises", "Edge case"),
        ),
        (
            "test",
            (
                "Write pytest unit tests for normalize_status(status: str) -> bool "
                "covering valid statuses and invalid predicate cases."
            ),
            _PREDICATE_TEST_ARTIFACT,
            ("@pytest.mark.unit", "None", "unknown", "False"),
        ),
        (
            "code_generation",
            "Generate normalize_status(value: str) and concise pytest tests.",
            _CODE_GENERATION_ARTIFACT,
            ("def normalize_status", "def test_", "with pytest.raises"),
        ),
    ],
)
def test_delegation_chain_returns_useful_task_artifact(
    task_type: str,
    prompt: str,
    artifact: str,
    expected_markers: tuple[str, ...],
) -> None:
    workflow = HandlerDelegationWorkflow(workflows={})
    request = ModelDelegationRequest(
        prompt=prompt,
        task_type=task_type,  # type: ignore[arg-type]
        correlation_id=uuid4(),
        max_tokens=4096,
        emitted_at=datetime.now(UTC),
    )

    routing_intents = workflow.handle_delegation_request(request)
    decision = HandlerRoutingIntent().handle(routing_intents[0])

    assert decision.task_type == task_type
    assert "final_artifact_only" in decision.dod_deterministic

    inference_intents = workflow.handle_routing_decision(decision)
    assert len(inference_intents) == 1
    assert isinstance(inference_intents[0], ModelInferenceIntent)

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = _httpx_response(artifact)
        mock_client_cls.return_value = mock_client
        response = HandlerInferenceIntent().handle(inference_intents[0])

    assert response.error_message == ""
    assert response.content == artifact.strip()
    for marker in expected_markers:
        assert marker in response.content

    gate_intents = workflow.handle_inference_response(response)
    assert len(gate_intents) == 1
    assert isinstance(gate_intents[0], ModelQualityGateIntent)

    gate_result = HandlerQualityGateIntent().handle(gate_intents[0])

    assert gate_result.passed is True
    assert gate_result.failure_reasons == ()
