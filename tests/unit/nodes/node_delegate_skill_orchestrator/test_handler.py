# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDelegateSkill."""

from __future__ import annotations

import inspect
from inspect import Parameter
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_handler_resolution_outcome import (
    EnumHandlerResolutionOutcome,
)
from omnibase_core.models.resolver.model_handler_resolver_context import (
    ModelHandlerResolverContext,
)
from omnibase_core.services.service_handler_resolver import ServiceHandlerResolver

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)


@pytest.fixture
def mock_dispatch_port() -> AsyncMock:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "completed",
        "content": "Generated test code...",
        "delegated_to": "qwen-coder",
        "model_name": "Qwen3-Coder-30B",
        "quality_gate_passed": True,
        "cost_usd": 0.001,
        "cost_savings_usd": 0.15,
        "delegation_latency_ms": 2500,
    }
    return port


@pytest.fixture
def event_bus() -> object:
    return object()


@pytest.mark.unit
async def test_handler_dispatches_and_returns_typed_response(
    mock_dispatch_port: AsyncMock,
    event_bus: object,
) -> None:
    handler = HandlerDelegateSkill(event_bus, dispatch_port=mock_dispatch_port)
    request = ModelDelegateSkillRequest(
        prompt="Write tests for payment webhook",
        task_type="test",
        source="claude-code",
        quality_contract_mode="replace_task_class",
        acceptance_criteria=("exactly_two_sentences",),
    )
    response = await handler.handle(request)
    assert response.status == "completed"
    assert response.task_type == "test"
    assert response.provider == "qwen-coder"
    assert response.model_name == "Qwen3-Coder-30B"
    assert response.quality_gate_passed is True
    assert response.response == "Generated test code..."
    assert response.metrics.cost_usd == 0.001
    assert response.metrics.cost_savings_usd == 0.15
    assert response.metrics.latency_ms == 2500
    mock_dispatch_port.dispatch.assert_awaited_once()
    call_kwargs = mock_dispatch_port.dispatch.await_args.kwargs
    assert call_kwargs["quality_contract_mode"] == "replace_task_class"
    assert call_kwargs["acceptance_criteria"] == ("exactly_two_sentences",)


@pytest.mark.unit
async def test_handler_propagates_correlation_id(
    mock_dispatch_port: AsyncMock,
    event_bus: object,
) -> None:
    cid = uuid4()
    handler = HandlerDelegateSkill(event_bus, dispatch_port=mock_dispatch_port)
    request = ModelDelegateSkillRequest(
        prompt="Document auth flow",
        task_type="document",
        source="codex",
        correlation_id=cid,
    )
    response = await handler.handle(request)
    assert response.correlation_id == cid
    mock_dispatch_port.dispatch.assert_awaited_once()
    call_kwargs = mock_dispatch_port.dispatch.await_args.kwargs
    assert call_kwargs["correlation_id"] == cid


@pytest.mark.unit
async def test_handler_returns_failed_on_dispatch_error() -> None:
    port = AsyncMock()
    port.dispatch.side_effect = RuntimeError("Connection refused")
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="test",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "failed"
    assert "Connection refused" in response.error_message
    assert response.task_type == "test"


@pytest.mark.unit
async def test_handler_maps_unknown_status_to_failed() -> None:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "weird-runtime-state",
        "content": "partial output",
    }
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="test",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "failed"
    assert "weird-runtime-state" in response.error_message


@pytest.mark.unit
async def test_handler_propagates_runtime_error_message() -> None:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "failed",
        "error_message": "model unavailable",
    }
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="test",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "failed"
    assert response.error_message == "model unavailable"


@pytest.mark.unit
async def test_handler_maps_quality_failure_reason() -> None:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "failed",
        "failure_reason": "TASK_MISMATCH: expected exactly 2 sentences, found 5",
        "quality_passed": False,
    }
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="document",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "failed"
    assert (
        response.error_message == "TASK_MISMATCH: expected exactly 2 sentences, found 5"
    )
    assert response.quality_gates_failed == [
        "TASK_MISMATCH: expected exactly 2 sentences, found 5"
    ]


@pytest.mark.unit
async def test_handler_maps_internal_delegation_result_fields() -> None:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "completed",
        "content": "internal result",
        "endpoint_url": "https://qwen.local",
        "model_used": "Qwen3-Coder-30B",
        "quality_passed": True,
        "latency_ms": 1234,
        "prompt_tokens": 12,
        "completion_tokens": 34,
    }
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="test",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "completed"
    assert response.provider == "https://qwen.local"
    assert response.model_name == "Qwen3-Coder-30B"
    assert response.response == "internal result"
    assert response.quality_gate_passed is True
    assert response.metrics.latency_ms == 1234
    assert response.metrics.input_tokens == 12
    assert response.metrics.output_tokens == 34


@pytest.mark.unit
async def test_handler_does_not_reference_transport_internals(
    mock_dispatch_port: AsyncMock,
) -> None:
    module = inspect.getmodule(HandlerDelegateSkill)
    assert module is not None
    source = inspect.getsource(module)
    forbidden = [
        "pattern_b",
        "pattern b",
        "kafka",
        "topic",
        "codex",
        "response_topic",
        "command_topic",
    ]
    lowered = source.lower()
    for word in forbidden:
        assert word not in lowered, (
            f"Handler module references transport detail: {word}"
        )


@pytest.mark.unit
def test_handler_constructor_allows_runtime_event_bus_and_zero_arg_default() -> None:
    signature = inspect.signature(HandlerDelegateSkill)
    required = {
        name
        for name, parameter in signature.parameters.items()
        if name != "self"
        and parameter.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
        and parameter.default is Parameter.empty
    }
    assert required == set()
    assert signature.parameters["event_bus"].default is None


@pytest.mark.unit
def test_handler_resolves_through_runtime_handler_resolver_zero_arg_default() -> None:
    context = ModelHandlerResolverContext(
        handler_cls=HandlerDelegateSkill,
        handler_module=HandlerDelegateSkill.__module__,
        handler_name="HandlerDelegateSkill",
        contract_name="node_delegate_skill_orchestrator",
        node_name="node_delegate_skill_orchestrator",
        event_bus=object(),
    )
    resolution = ServiceHandlerResolver().resolve(context)
    assert resolution.outcome is EnumHandlerResolutionOutcome.RESOLVED_VIA_ZERO_ARG
    assert isinstance(resolution.handler_instance, HandlerDelegateSkill)


@pytest.mark.unit
def test_handler_constructs_without_dispatch_port() -> None:
    handler = HandlerDelegateSkill()
    assert handler is not None


@pytest.mark.unit
async def test_handler_with_no_port_fails_closed_on_dispatch() -> None:
    handler = HandlerDelegateSkill()
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="test",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "failed"
    assert "no dispatch port wired" in response.error_message.lower()
