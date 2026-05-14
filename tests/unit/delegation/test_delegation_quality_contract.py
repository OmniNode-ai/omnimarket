# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven quality checks for delegation output."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_quality_gate_intent import (
    ModelQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)


def _backend_id(name: str) -> UUID:
    return uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{name}")


@pytest.fixture(autouse=True)
def _bifrost_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handler_delegation_routing._config = None
    handler_delegation_routing._load_bifrost_endpoints.cache_clear()
    handler_delegation_routing._get_task_class_contract.cache_clear()
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(
        "config_version: '1.1.0'\n"
        "schema_version: bifrost_delegation.v1\n"
        "backends:\n"
        "  - backend_id: local-deepseek-r1-14b\n"
        '    endpoint_url: "http://test-document:8001"\n'
        '    model_name: "test-model-placeholder"\n'
        "    tier: local\n"
        "    timeout_ms: 30000\n"
        "    capabilities: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    yield
    handler_delegation_routing._config = None
    handler_delegation_routing._load_bifrost_endpoints.cache_clear()
    handler_delegation_routing._get_task_class_contract.cache_clear()


@pytest.mark.unit
def test_routing_decision_carries_document_contract_dod() -> None:
    request = ModelDelegationRequest(
        prompt="Write a Google-style docstring for a runtime payload validator.",
        task_type="document",
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
    )

    decision = handler_delegation_routing.delta(request)

    assert decision.dod_deterministic == ("docstring_present",)
    assert decision.dod_heuristic == (
        "no_refusal",
        "follows_google_style",
        "covers_args_returns_raises",
    )


@pytest.mark.unit
def test_workflow_forwards_routing_dod_to_quality_gate_input() -> None:
    handler = HandlerDelegationWorkflow()
    correlation_id = uuid4()

    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt="Write a Google-style docstring.",
            task_type="document",
            correlation_id=correlation_id,
            emitted_at=datetime.now(UTC),
        )
    )
    handler.handle_routing_decision(
        ModelRoutingDecision(
            correlation_id=correlation_id,
            task_type="document",
            selected_model="deepseek-r1-14b",
            selected_backend_id=_backend_id("deepseek-r1-14b"),
            endpoint_url="http://test-llm:8000",
            cost_tier="low",
            max_context_tokens=24576,
            system_prompt="Write documentation.",
            rationale="test",
            dod_deterministic=("docstring_present",),
            dod_heuristic=(
                "no_refusal",
                "follows_google_style",
                "covers_args_returns_raises",
            ),
        )
    )

    intents = handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=correlation_id,
            content='"""Validate a payload.\n\nArgs:\n    payload: Raw payload.\n\nReturns:\n    Normalized payload.\n\nRaises:\n    ValueError: On invalid payload.\n"""',
            model_used="deepseek-r1-14b",
        )
    )

    assert len(intents) == 1
    assert isinstance(intents[0], ModelQualityGateIntent)
    gate_input = intents[0].payload
    assert gate_input.dod_deterministic == ("docstring_present",)
    assert gate_input.dod_heuristic == (
        "no_refusal",
        "follows_google_style",
        "covers_args_returns_raises",
    )


@pytest.mark.unit
def test_workflow_forwards_request_acceptance_criteria() -> None:
    handler = HandlerDelegationWorkflow()
    correlation_id = uuid4()

    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt="Return exactly two short sentences.",
            task_type="document",
            correlation_id=correlation_id,
            emitted_at=datetime.now(UTC),
            quality_contract_mode="replace_task_class",
            acceptance_criteria=(
                "exactly_two_sentences",
                "max_words_per_sentence_20",
                "plain_text_only",
            ),
        )
    )
    handler.handle_routing_decision(
        ModelRoutingDecision(
            correlation_id=correlation_id,
            task_type="document",
            selected_model="deepseek-r1-14b",
            selected_backend_id=_backend_id("deepseek-r1-14b"),
            endpoint_url="http://test-llm:8000",
            cost_tier="low",
            max_context_tokens=24576,
            system_prompt="Write documentation.",
            rationale="test",
            dod_deterministic=("docstring_present",),
            dod_heuristic=("no_refusal",),
        )
    )

    intents = handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=correlation_id,
            content="One short sentence. Another short sentence.",
            model_used="deepseek-r1-14b",
        )
    )

    assert isinstance(intents[0], ModelQualityGateIntent)
    gate_input = intents[0].payload
    assert gate_input.quality_contract_mode == "replace_task_class"
    assert gate_input.acceptance_criteria == (
        "exactly_two_sentences",
        "max_words_per_sentence_20",
        "plain_text_only",
    )


@pytest.mark.unit
def test_document_quality_gate_rejects_prompt_mismatched_code_output() -> None:
    gate_input = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="document",
        llm_response_content=(
            "```python\ndef validate_payload(payload):\n    return payload\n```"
        ),
        dod_deterministic=("docstring_present",),
        dod_heuristic=(
            "no_refusal",
            "follows_google_style",
            "covers_args_returns_raises",
        ),
    )

    result = quality_gate_delta(gate_input)

    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert "TASK_MISMATCH: missing docstring" in result.failure_reasons


@pytest.mark.unit
def test_test_quality_gate_rejects_missing_pytest_unit_marker() -> None:
    gate_input = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="test",
        llm_response_content="def test_payload_validation():\n    assert True\n",
        dod_deterministic=("compiles_without_errors", "uses_pytest_mark_unit"),
        dod_heuristic=("covers_edge_cases", "covers_error_paths"),
    )

    result = quality_gate_delta(gate_input)

    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert "TASK_MISMATCH: missing @pytest.mark.unit" in result.failure_reasons


@pytest.mark.unit
def test_request_quality_contract_can_replace_task_class_dod() -> None:
    gate_input = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="document",
        llm_response_content="First sentence is short. Second sentence is short.",
        dod_deterministic=("docstring_present",),
        dod_heuristic=("no_refusal",),
        quality_contract_mode="replace_task_class",
        acceptance_criteria=(
            "exactly_two_sentences",
            "max_words_per_sentence_20",
            "plain_text_only",
        ),
    )

    result = quality_gate_delta(gate_input)

    assert result.passed is True


@pytest.mark.unit
def test_request_quality_contract_rejects_output_shape_mismatch() -> None:
    gate_input = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="document",
        llm_response_content=(
            "```python\n"
            "def validate_payload(payload):\n"
            '    """Validate the payload. Args: payload. Returns: bool."""\n'
            "    return True\n"
            "```"
        ),
        quality_contract_mode="replace_task_class",
        acceptance_criteria=(
            "exactly_two_sentences",
            "max_words_per_sentence_20",
            "plain_text_only",
        ),
    )

    result = quality_gate_delta(gate_input)

    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert any("expected plain text" in r for r in result.failure_reasons)
