# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDelegateSkill."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

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


@pytest.mark.unit
async def test_handler_dispatches_and_returns_typed_response(
    mock_dispatch_port: AsyncMock,
) -> None:
    handler = HandlerDelegateSkill(dispatch_port=mock_dispatch_port)
    request = ModelDelegateSkillRequest(
        prompt="Write tests for payment webhook",
        task_type="test",
        source="claude-code",
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


@pytest.mark.unit
async def test_handler_propagates_correlation_id(
    mock_dispatch_port: AsyncMock,
) -> None:
    cid = uuid4()
    handler = HandlerDelegateSkill(dispatch_port=mock_dispatch_port)
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
    handler = HandlerDelegateSkill(dispatch_port=port)
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
    handler = HandlerDelegateSkill(dispatch_port=port)
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
    handler = HandlerDelegateSkill(dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="test",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "failed"
    assert response.error_message == "model unavailable"


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
