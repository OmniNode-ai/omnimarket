# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Downstream preservation of caller-owned cloud request semantics (OMN-15539)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.models.delegation.wire import (
    ModelBudgetLimits,
    ModelDelegationRequest,
    ModelInferenceIntent,
    ModelInferenceResponseData,
    ModelQualityGateInput,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)

from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as workflow_module,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)

_CALLER_SYSTEM_PROMPT = "Follow the caller's exact structured-output instructions."
_ROUTING_SYSTEM_PROMPT = "Task-class routing default that must not win."
_RESPONSE_FORMAT: dict[str, object] = {"type": "json_object"}
_RESPONSE_CONTRACT: dict[str, object] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _request(
    correlation_id: UUID,
    *,
    compliance: bool = False,
) -> ModelDelegationRequest:
    kwargs: dict[str, object] = {
        "prompt": "Return a structured answer.",
        "task_type": "review" if compliance else "test",
        "correlation_id": correlation_id,
        "emitted_at": datetime.now(UTC),
        "system_prompt": _CALLER_SYSTEM_PROMPT,
        # A falsey explicit value proves precedence uses ``is not None``.
        "temperature": 0.0,
        "response_format": _RESPONSE_FORMAT,
        "response_contract": _RESPONSE_CONTRACT,
    }
    if compliance:
        kwargs.update(
            output_schema_key="review_output",
            compliance_budget=ModelBudgetLimits(
                max_tokens=100_000,
                max_cost_usd=100.0,
                max_time_s=1_000.0,
            ),
        )
    return ModelDelegationRequest.model_validate(kwargs)


def _decision(
    correlation_id: UUID,
    *,
    task_type: str = "test",
    tier_name: str = "local",
    model: str = "provider-model-without-protocol-overrides",
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type=task_type,
        selected_model=model,
        selected_backend_id=uuid5(NAMESPACE_DNS, f"omn15539/{tier_name}/{model}"),
        endpoint_url="https://provider.example/v1/chat/completions",
        cost_tier="low",
        max_context_tokens=65_536,
        max_tokens=4_096,
        system_prompt=_ROUTING_SYSTEM_PROMPT,
        rationale="Focused OMN-15539 test routing decision.",
        tier_name=tier_name,
    )


def _response(
    correlation_id: UUID,
    content: str,
) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content=content,
        model_used="provider-model-without-protocol-overrides",
        latency_ms=5,
        prompt_tokens=2,
        completion_tokens=3,
        total_tokens=5,
    )


def _assert_caller_provider_semantics(intent: ModelInferenceIntent) -> None:
    assert intent.system_prompt == _CALLER_SYSTEM_PROMPT
    assert intent.temperature == 0.0
    assert intent.response_format == _RESPONSE_FORMAT


@pytest.mark.unit
def test_initial_inference_preserves_caller_provider_semantics() -> None:
    handler = HandlerDelegationWorkflow(workflows={})
    correlation_id = uuid4()
    handler.handle_delegation_request(_request(correlation_id))

    intents = handler.handle_routing_decision(_decision(correlation_id))

    assert len(intents) == 1
    _assert_caller_provider_semantics(intents[0])


@pytest.mark.unit
def test_escalated_inference_preserves_caller_provider_semantics(
    monkeypatch: pytest.MonkeyPatch,
    frontier_unconfigured_bifrost: None,
) -> None:
    monkeypatch.setattr(workflow_module, "is_free_tier", lambda _tier: False)
    handler = HandlerDelegationWorkflow(workflows={})
    correlation_id = uuid4()
    handler.handle_delegation_request(_request(correlation_id))
    handler.handle_routing_decision(_decision(correlation_id, tier_name="local"))
    gate_intents = handler.handle_inference_response(
        _response(correlation_id, "first attempt")
    )
    assert isinstance(gate_intents[0], ModelQualityGateIntent)

    escalation_events = handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=correlation_id,
            passed=False,
            quality_score=0.0,
            failure_reasons=("quality below caller requirements",),
            fallback_recommended=True,
        ),
        max_escalation_attempts=2,
    )
    reroute = next(
        event for event in escalation_events if isinstance(event, ModelRoutingIntent)
    )
    assert reroute.min_tier_name is not None

    escalated_intents = handler.handle_routing_decision(
        _decision(
            correlation_id,
            tier_name=reroute.min_tier_name,
            model="escalated-provider-model",
        )
    )

    assert len(escalated_intents) == 1
    _assert_caller_provider_semantics(escalated_intents[0])


@pytest.mark.unit
def test_compliance_repair_preserves_provider_semantics_and_gate_contract() -> None:
    handler = HandlerDelegationWorkflow(workflows={})
    correlation_id = uuid4()
    request = _request(correlation_id, compliance=True)
    handler.handle_delegation_request(request)
    initial = handler.handle_routing_decision(
        _decision(correlation_id, task_type=request.task_type)
    )
    _assert_caller_provider_semantics(initial[0])

    repair = handler.handle_inference_response(_response(correlation_id, "{}"))

    assert len(repair) == 1
    assert isinstance(repair[0], ModelInferenceIntent)
    _assert_caller_provider_semantics(repair[0])

    valid_review = json.dumps(
        {"verdict": "approve", "summary": "Meets requirements", "findings": []}
    )
    gate_events = handler.handle_inference_response(
        _response(correlation_id, valid_review)
    )

    assert len(gate_events) == 1
    assert isinstance(gate_events[0], ModelQualityGateIntent)
    assert gate_events[0].payload.response_contract == _RESPONSE_CONTRACT


@pytest.mark.unit
def test_legacy_quality_gate_input_carries_request_response_contract() -> None:
    handler = HandlerDelegationWorkflow(workflows={})
    correlation_id = uuid4()
    handler.handle_delegation_request(_request(correlation_id))
    handler.handle_routing_decision(_decision(correlation_id))

    gate_events = handler.handle_inference_response(
        _response(correlation_id, '{"answer": "ok"}')
    )

    assert len(gate_events) == 1
    assert isinstance(gate_events[0], ModelQualityGateIntent)
    assert gate_events[0].payload.response_contract == _RESPONSE_CONTRACT


def _explicit_contract_intent() -> ModelQualityGateIntent:
    return ModelQualityGateIntent(
        payload=ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="agent_delegation",
            llm_response_content='{"answer": "ok"}',
            response_contract=_RESPONSE_CONTRACT,
        )
    )


@pytest.mark.unit
def test_quality_gate_sync_prefers_explicit_contract_over_task_default() -> None:
    result = HandlerQualityGateIntent().handle(_explicit_contract_intent())

    assert result.passed is True
    assert result.failure_reasons == ()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quality_gate_async_prefers_explicit_contract_over_task_default() -> None:
    output = await HandlerQualityGateIntent().handle_async(_explicit_contract_intent())
    result = next(
        event for event in output.events if isinstance(event, ModelQualityGateResult)
    )

    assert result.passed is True
    assert result.failure_reasons == ()


def _provider_intent(**overrides: object) -> ModelInferenceIntent:
    payload: dict[str, object] = {
        "base_url": "https://provider.example/v1/chat/completions",
        "model": "provider-model-without-protocol-overrides",
        "system_prompt": _CALLER_SYSTEM_PROMPT,
        "prompt": "Return a structured answer.",
        "max_tokens": 512,
        "temperature": 0.0,
        "timeout_seconds": 30.0,
        "correlation_id": uuid4(),
    }
    payload.update(overrides)
    return ModelInferenceIntent.model_validate(payload)


@pytest.mark.unit
def test_inference_handler_places_typed_response_format_on_provider_payload() -> None:
    intent = _provider_intent(response_format=_RESPONSE_FORMAT)
    provider_response = MagicMock()
    provider_response.raise_for_status.return_value = None
    provider_response.json.return_value = {
        "id": "response-omn15539",
        "choices": [
            {"finish_reason": "stop", "message": {"content": '{"answer": "ok"}'}}
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }

    with patch("httpx.Client") as client_class:  # onex-allow-faked-boundary
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.return_value = provider_response
        client_class.return_value = client

        result = HandlerInferenceIntent().handle(intent)

    assert result.error_message == ""
    provider_payload = client.post.call_args.kwargs["json"]
    assert provider_payload["response_format"] == _RESPONSE_FORMAT


@pytest.mark.unit
def test_inference_handler_forbids_response_format_options_collision() -> None:
    intent = _provider_intent(
        response_format=_RESPONSE_FORMAT,
        provider_request_options={"response_format": {"type": "text"}},
    )

    with patch("httpx.Client") as client_class:  # onex-allow-faked-boundary
        result = HandlerInferenceIntent().handle(intent)

    assert result.content == ""
    assert result.error_message == (
        "provider request options cannot override: response_format"
    )
    client_class.assert_not_called()
