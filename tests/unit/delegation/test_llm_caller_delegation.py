# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Unit tests for LlmCallerDelegation.

Verifies the adapter correctly translates ModelInferenceIntent to
ModelInferenceResponseData without hitting a real LLM endpoint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from omnibase_infra.adapters.llm.model_llm_adapter_response import (
    ModelLlmAdapterResponse,
)

from omnimarket.adapters.llm.adapter_llm_caller_delegation import (
    LlmCallerDelegation,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)


def _make_intent(
    *,
    model: str = "qwen3-coder-30b",
    base_url: str = "http://192.168.86.201:8000/v1",  # onex-allow-internal-ip: delegation test fixture for local LLM endpoint
    system_prompt: str = "You are a code generation assistant.",
    prompt: str = "Write a pytest test for the add() function.",
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> ModelInferenceIntent:
    return ModelInferenceIntent(
        base_url=base_url,
        model=model,
        system_prompt=system_prompt,
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        correlation_id=uuid4(),
    )


def _make_adapter_response(
    generated_text: str = "def test_add():\n    assert add(1, 2) == 3",
    model_used: str = "qwen3-coder-30b",
) -> ModelLlmAdapterResponse:
    return ModelLlmAdapterResponse(
        generated_text=generated_text,
        model_used=model_used,
        usage_statistics={
            "prompt_tokens": 50,
            "completion_tokens": 30,
            "total_tokens": 80,
        },
        finish_reason="stop",
        response_metadata={"latency_ms": 42, "provider_id": "", "correlation_id": ""},
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_returns_inference_response_data() -> None:
    """LlmCallerDelegation.call() returns a well-formed ModelInferenceResponseData."""
    intent = _make_intent()
    adapter_response = _make_adapter_response()
    caller = LlmCallerDelegation()

    with patch(
        "omnimarket.adapters.llm.adapter_llm_caller_delegation.AdapterLlmProviderOpenai"
    ) as mock_provider:
        instance = MagicMock()
        instance.generate_async = AsyncMock(return_value=adapter_response)
        instance.close = AsyncMock()
        mock_provider.return_value = instance

        result = await caller.call(intent)

    assert isinstance(result, ModelInferenceResponseData)
    assert result.correlation_id == intent.correlation_id
    assert result.content == adapter_response.generated_text
    assert result.model_used == adapter_response.model_used
    assert result.prompt_tokens == 50
    assert result.completion_tokens == 30
    assert result.total_tokens == 80


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_prepends_system_prompt() -> None:
    """System prompt is prepended to user prompt when calling the provider."""
    intent = _make_intent(system_prompt="Be concise.", prompt="Write a function.")
    adapter_response = _make_adapter_response()
    caller = LlmCallerDelegation()

    captured_request = {}

    async def _capture(req: object) -> ModelLlmAdapterResponse:
        captured_request["prompt"] = getattr(req, "prompt", "")
        return adapter_response

    with patch(
        "omnimarket.adapters.llm.adapter_llm_caller_delegation.AdapterLlmProviderOpenai"
    ) as mock_provider:
        instance = MagicMock()
        instance.generate_async = _capture
        instance.close = AsyncMock()
        mock_provider.return_value = instance

        await caller.call(intent)

    assert "Be concise." in captured_request["prompt"]
    assert "Write a function." in captured_request["prompt"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_without_system_prompt() -> None:
    """When system_prompt is empty, only the user prompt is sent."""
    intent = _make_intent(system_prompt="", prompt="Just the user prompt.")
    adapter_response = _make_adapter_response()
    caller = LlmCallerDelegation()

    captured_request = {}

    async def _capture(req: object) -> ModelLlmAdapterResponse:
        captured_request["prompt"] = getattr(req, "prompt", "")
        return adapter_response

    with patch(
        "omnimarket.adapters.llm.adapter_llm_caller_delegation.AdapterLlmProviderOpenai"
    ) as mock_provider:
        instance = MagicMock()
        instance.generate_async = _capture
        instance.close = AsyncMock()
        mock_provider.return_value = instance

        await caller.call(intent)

    assert captured_request["prompt"] == "Just the user prompt."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_closed_after_call() -> None:
    """Provider.close() is called even when generate_async raises."""
    intent = _make_intent()
    caller = LlmCallerDelegation()

    with patch(
        "omnimarket.adapters.llm.adapter_llm_caller_delegation.AdapterLlmProviderOpenai"
    ) as mock_provider:
        instance = MagicMock()
        instance.generate_async = AsyncMock(side_effect=RuntimeError("timeout"))
        instance.close = AsyncMock()
        mock_provider.return_value = instance

        with pytest.raises(RuntimeError, match="timeout"):
            await caller.call(intent)

        instance.close.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_constructed_with_correct_base_url() -> None:
    """AdapterLlmProviderOpenai is constructed with intent.base_url."""
    intent = _make_intent(
        base_url="http://192.168.86.201:8001/v1",  # onex-allow-internal-ip: delegation test fixture for local LLM endpoint
        model="deepseek-r1-14b",
    )
    adapter_response = _make_adapter_response(model_used="deepseek-r1-14b")
    caller = LlmCallerDelegation()

    with patch(
        "omnimarket.adapters.llm.adapter_llm_caller_delegation.AdapterLlmProviderOpenai"
    ) as mock_provider:
        instance = MagicMock()
        instance.generate_async = AsyncMock(return_value=adapter_response)
        instance.close = AsyncMock()
        mock_provider.return_value = instance

        await caller.call(intent)

        mock_provider.assert_called_once()
        call_kwargs = mock_provider.call_args.kwargs
        assert (
            call_kwargs["base_url"]
            == "http://192.168.86.201:8001/v1"  # onex-allow-internal-ip: delegation test fixture
        )
        assert call_kwargs["default_model"] == "deepseek-r1-14b"
