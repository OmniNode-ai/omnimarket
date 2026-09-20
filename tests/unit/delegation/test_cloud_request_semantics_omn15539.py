# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Downstream preservation of caller-owned cloud request semantics (OMN-15539)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from unittest.mock import MagicMock, patch
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.models.delegation.wire import (
    EnumDelegationOutputShape,
    ModelBudgetLimits,
    ModelDelegationCompleted,
    ModelDelegationRequest,
    ModelInferenceIntent,
    ModelInferenceResponseData,
    ModelQualityGateInput,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)

from omnimarket.delegation.response_contract_instruction import (
    compose_system_prompt_with_response_contract,
    render_response_contract_instruction,
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
_REVIEW_RESPONSE_CONTRACT: dict[str, object] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["approve", "request_changes", "comment"],
        },
        "summary": {"type": "string"},
        "findings": {"type": "array"},
    },
    "required": ["verdict", "summary", "findings"],
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
        "response_contract": (
            _REVIEW_RESPONSE_CONTRACT if compliance else _RESPONSE_CONTRACT
        ),
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


def _assert_caller_provider_semantics(
    intent: ModelInferenceIntent,
    response_contract: dict[str, object] = _RESPONSE_CONTRACT,
) -> None:
    assert intent.system_prompt == compose_system_prompt_with_response_contract(
        system_prompt=_CALLER_SYSTEM_PROMPT,
        response_contract=response_contract,
    )
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
        _response(correlation_id, '{"answer":"first attempt"}')
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
    _assert_caller_provider_semantics(initial[0], _REVIEW_RESPONSE_CONTRACT)

    repair = handler.handle_inference_response(_response(correlation_id, "{}"))

    assert len(repair) == 1
    assert isinstance(repair[0], ModelInferenceIntent)
    _assert_caller_provider_semantics(repair[0], _REVIEW_RESPONSE_CONTRACT)
    assert handler._workflows[correlation_id].output_refusal is not None

    valid_review = json.dumps(
        {"verdict": "approve", "summary": "Meets requirements", "findings": []}
    )
    gate_events = handler.handle_inference_response(
        _response(correlation_id, valid_review)
    )

    assert len(gate_events) == 1
    assert isinstance(gate_events[0], ModelQualityGateIntent)
    assert gate_events[0].payload.response_contract == _REVIEW_RESPONSE_CONTRACT

    terminal_events = handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=correlation_id,
            passed=True,
            quality_score=1.0,
        )
    )

    assert len(terminal_events) == 1
    terminal = terminal_events[0]
    assert isinstance(terminal, ModelDelegationCompleted)
    assert terminal.content == valid_review
    assert terminal.output_refusal is None
    assert terminal.compliance_attempts == 2
    # One initial inference plus one schema-repair inference really ran. This
    # total must not collapse to escalation_history + 1 (there was no tier
    # escalation, so that older formula reported the contradictory value 1).
    assert terminal.attempts_count == 2


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
    evidence = gate_events[0].payload.deliverable_evidence
    assert evidence is not None
    assert evidence.output_shape is EnumDelegationOutputShape.JSON
    assert (
        evidence.contract_sha256
        == handler._workflows[correlation_id].response_contract_sha256
    )
    assert evidence.deliverable_sha256 == sha256(b'{"answer": "ok"}').hexdigest()
    assert evidence.deliverable_chars == len('{"answer": "ok"}')
    assert evidence.preamble_chars == 0
    assert evidence.raw_chars == len('{"answer": "ok"}')
    assert evidence.deliverable_start == 0
    assert evidence.deliverable_end == len('{"answer": "ok"}')


@pytest.mark.unit
def test_unmarked_default_text_response_reaches_the_gate_as_empty_typed_evidence() -> (
    None
):
    handler = HandlerDelegationWorkflow(workflows={})
    correlation_id = uuid4()
    request = _request(correlation_id).model_copy(update={"response_contract": None})
    handler.handle_delegation_request(request)
    handler.handle_routing_decision(_decision(correlation_id))

    events = handler.handle_inference_response(
        _response(correlation_id, "raw response without the declared marker")
    )

    assert len(events) == 1
    assert isinstance(events[0], ModelQualityGateIntent)
    assert events[0].payload.llm_response_content == ""
    workflow = handler._workflows[correlation_id]
    assert workflow.output_refusal is not None
    assert workflow.output_refusal.reason == "ambiguous_unmarked_deliverable"


@pytest.mark.unit
def test_class_default_contract_reaches_initial_provider_intent_and_gate() -> None:
    """The model and gate share the resolved class default, not raw ``None``."""
    handler = HandlerDelegationWorkflow(workflows={})
    correlation_id = uuid4()
    request = _request(correlation_id)
    request = request.model_copy(
        update={"task_type": "agent_delegation", "response_contract": None}
    )
    handler.handle_delegation_request(request)

    intents = handler.handle_routing_decision(
        _decision(correlation_id, task_type="agent_delegation")
    )

    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, ModelInferenceIntent)
    assert "JSON Schema" in intent.system_prompt

    gate_events = handler.handle_inference_response(
        _response(correlation_id, '{"status": "completed"}')
    )
    assert len(gate_events) == 1
    assert isinstance(gate_events[0], ModelQualityGateIntent)
    assert gate_events[0].payload.response_contract is not None
    assert gate_events[0].payload.response_contract != _RESPONSE_CONTRACT


@pytest.mark.unit
def test_caller_contract_wins_over_class_default_on_provider_intent_and_gate() -> None:
    """An explicit caller schema is the exact schema shown and validated."""
    handler = HandlerDelegationWorkflow(workflows={})
    correlation_id = uuid4()
    request = _request(correlation_id).model_copy(
        update={"task_type": "agent_delegation"}
    )
    handler.handle_delegation_request(request)

    intents = handler.handle_routing_decision(
        _decision(correlation_id, task_type="agent_delegation")
    )

    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, ModelInferenceIntent)
    assert "answer" in intent.system_prompt
    assert "DispatchReport" not in intent.system_prompt

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
def test_inference_handler_records_only_the_instruction_in_the_sent_payload() -> None:
    instruction = render_response_contract_instruction(_RESPONSE_CONTRACT)
    intent = _provider_intent(
        system_prompt=f"{_CALLER_SYSTEM_PROMPT}\n\n{instruction}",
        response_contract_instruction=instruction,
        response_contract_sha256=sha256(
            json.dumps(
                _RESPONSE_CONTRACT, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        response_contract_output_shape=EnumDelegationOutputShape.JSON,
    )
    provider_response = MagicMock()
    provider_response.raise_for_status.return_value = None
    provider_response.json.return_value = {
        "id": "response-contract-evidence",
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

    provider_payload = client.post.call_args.kwargs["json"]
    assert instruction in provider_payload["messages"][0]["content"]
    assert client.post.call_count == 1
    assert result.response_contract_evidence is not None
    assert result.response_contract_evidence.conveyed is True
    assert result.response_contract_evidence.validated is False
    assert result.response_contract_evidence.channel == "messages[0].content"


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
