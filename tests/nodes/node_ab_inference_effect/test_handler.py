# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-10580 reason="test fixture — uses lab LLM endpoint as allowed-endpoint test input and in request fixtures; not a runtime default"

"""Tests for HandlerAbInferenceEffect.

All network I/O is exercised via injected callables — no real HTTP calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_ab_inference_effect.handlers.handler_ab_inference_effect import (
    HandlerAbInferenceEffect,
)
from omnimarket.nodes.node_ab_inference_effect.models.model_inference_request import (
    ModelInferenceRequest,
)
from omnimarket.nodes.node_ab_inference_effect.models.model_inference_result import (
    ModelInferenceResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CORRELATION_ID = "test-corr-001"
ALLOWED_ENDPOINTS = {"http://localhost:8000", "http://192.168.86.201:8000"}


def _make_request(
    protocol: str = "openai_compatible",
    endpoint_url: str = "http://localhost:8000",
    model_id: str = "test-model",
    model_key: str = "test-model-key",
    prompt: str = "Hello",
    system_prompt: str = "",
    timeout_seconds: float = 30.0,
) -> ModelInferenceRequest:
    return ModelInferenceRequest(
        model_key=model_key,
        endpoint_url=endpoint_url,
        model_id=model_id,
        protocol=protocol,
        prompt=prompt,
        system_prompt=system_prompt,
        correlation_id=CORRELATION_ID,
        timeout_seconds=timeout_seconds,
    )


def _make_openai_response(
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
    content: str = "The answer is 42.",
) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _make_http_post_fn(
    response: dict[str, Any],
    status_code: int = 200,
) -> Any:
    async def _post(
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        return status_code, response

    return _post


# ---------------------------------------------------------------------------
# openai_compatible protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_openai_compatible_happy_path() -> None:
    """Correct token counts and content extracted from a standard OpenAI response."""
    response = _make_openai_response(
        prompt_tokens=15, completion_tokens=25, content="Hello world"
    )
    handler = HandlerAbInferenceEffect(
        http_post_fn=_make_http_post_fn(response),
        allowed_endpoint_urls=ALLOWED_ENDPOINTS,
    )
    req = _make_request(protocol="openai_compatible")

    result = await handler.handle(req)

    assert isinstance(result, ModelInferenceResult)
    assert result.error == ""
    assert result.prompt_tokens == 15
    assert result.completion_tokens == 25
    assert result.total_tokens == 40
    assert result.raw_output == "Hello world"
    assert result.usage_source == EnumUsageSource.MEASURED
    assert result.correlation_id == CORRELATION_ID
    assert result.model_key == "test-model-key"
    assert result.latency_ms >= 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_openai_compatible_url_constructed_correctly() -> None:
    """The HTTP POST must target {endpoint_url}/v1/chat/completions."""
    captured_urls: list[str] = []
    response = _make_openai_response()

    async def _capturing_post(
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        captured_urls.append(url)
        return 200, response

    handler = HandlerAbInferenceEffect(
        http_post_fn=_capturing_post,
        allowed_endpoint_urls=ALLOWED_ENDPOINTS,
    )
    req = _make_request(endpoint_url="http://192.168.86.201:8000")
    await handler.handle(req)

    assert len(captured_urls) == 1
    assert captured_urls[0] == "http://192.168.86.201:8000/v1/chat/completions"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_openai_compatible_system_prompt_included() -> None:
    """System prompt appears as first message when non-empty."""
    captured_payloads: list[dict[str, Any]] = []
    response = _make_openai_response()

    async def _capturing_post(
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        captured_payloads.append(payload)
        return 200, response

    handler = HandlerAbInferenceEffect(
        http_post_fn=_capturing_post,
        allowed_endpoint_urls=ALLOWED_ENDPOINTS,
    )
    req = _make_request(system_prompt="You are an expert.", prompt="Write code.")
    await handler.handle(req)

    messages = captured_payloads[0]["messages"]
    assert messages[0] == {"role": "system", "content": "You are an expert."}
    assert messages[1] == {"role": "user", "content": "Write code."}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_openai_compatible_no_system_prompt_omits_system_message() -> None:
    """When system_prompt is empty, only a user message is sent."""
    captured_payloads: list[dict[str, Any]] = []
    response = _make_openai_response()

    async def _capturing_post(
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        captured_payloads.append(payload)
        return 200, response

    handler = HandlerAbInferenceEffect(
        http_post_fn=_capturing_post,
        allowed_endpoint_urls=ALLOWED_ENDPOINTS,
    )
    req = _make_request(system_prompt="", prompt="Hello")
    await handler.handle(req)

    messages = captured_payloads[0]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_openai_compatible_missing_usage_marks_unknown() -> None:
    """When the API response has no usage field, usage_source is UNKNOWN."""
    response: dict[str, Any] = {
        "choices": [{"message": {"content": "some output"}}],
    }
    handler = HandlerAbInferenceEffect(
        http_post_fn=_make_http_post_fn(response),
        allowed_endpoint_urls=ALLOWED_ENDPOINTS,
    )
    req = _make_request()

    result = await handler.handle(req)

    assert result.usage_source == EnumUsageSource.UNKNOWN
    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
    assert result.error == ""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_openai_compatible_timeout_returns_error_result() -> None:
    """TimeoutException produces an error result with zero tokens — does not raise."""

    async def _timeout_post(
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        raise httpx.TimeoutException("timed out")

    handler = HandlerAbInferenceEffect(
        http_post_fn=_timeout_post,
        allowed_endpoint_urls=ALLOWED_ENDPOINTS,
    )
    req = _make_request()

    result = await handler.handle(req)

    assert result.error != ""
    assert "TimeoutException" in result.error
    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
    assert result.usage_source == EnumUsageSource.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.unit
async def test_openai_compatible_connect_error_returns_error_result() -> None:
    """ConnectError produces an error result — does not raise."""

    async def _connect_err_post(
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        raise httpx.ConnectError("connection refused")

    handler = HandlerAbInferenceEffect(
        http_post_fn=_connect_err_post,
        allowed_endpoint_urls=ALLOWED_ENDPOINTS,
    )
    req = _make_request()

    result = await handler.handle(req)

    assert result.error != ""
    assert result.prompt_tokens == 0
    assert result.usage_source == EnumUsageSource.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.unit
async def test_openai_compatible_rejects_untrusted_endpoint() -> None:
    """OpenAI-compatible calls must target a configured endpoint, not caller-supplied arbitrary URLs."""
    post = AsyncMock(return_value=(200, _make_openai_response()))
    handler = HandlerAbInferenceEffect(
        http_post_fn=post,
        allowed_endpoint_urls={"http://localhost:8000"},
    )
    req = _make_request(endpoint_url="http://169.254.169.254")

    result = await handler.handle(req)

    assert result.error != ""
    assert "allowlist" in result.error
    assert result.usage_source == EnumUsageSource.UNKNOWN
    post.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_openai_compatible_malformed_endpoint_port_returns_error() -> None:
    """Malformed endpoint ports must not escape the error-result boundary."""
    post = AsyncMock(return_value=(200, _make_openai_response()))
    handler = HandlerAbInferenceEffect(
        http_post_fn=post,
        allowed_endpoint_urls=ALLOWED_ENDPOINTS,
    )
    req = _make_request(endpoint_url="http://localhost:99999")

    result = await handler.handle(req)

    assert result.error != ""
    assert "allowlist" in result.error
    assert result.usage_source == EnumUsageSource.UNKNOWN
    post.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_openai_compatible_malformed_response_returns_error_result() -> None:
    """Malformed successful provider responses stay inside the error-result boundary."""
    response: dict[str, Any] = {
        "choices": [{"message": {"content": "some output"}}],
        "usage": {"prompt_tokens": object()},
    }
    handler = HandlerAbInferenceEffect(
        http_post_fn=_make_http_post_fn(response),
        allowed_endpoint_urls=ALLOWED_ENDPOINTS,
    )
    req = _make_request()

    result = await handler.handle(req)

    assert result.error != ""
    assert result.prompt_tokens == 0
    assert result.usage_source == EnumUsageSource.UNKNOWN


# ---------------------------------------------------------------------------
# anthropic protocol
# ---------------------------------------------------------------------------


def _make_anthropic_response(
    input_tokens: int = 12,
    output_tokens: int = 18,
    content_text: str = "Here is the answer.",
) -> Any:
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    content_block = MagicMock()
    content_block.text = content_text

    response = MagicMock()
    response.usage = usage
    response.content = [content_block]
    return response


@pytest.mark.asyncio
@pytest.mark.unit
async def test_anthropic_happy_path() -> None:
    """Correct input/output tokens extracted from anthropic SDK response."""
    fake_response = _make_anthropic_response(
        input_tokens=12, output_tokens=18, content_text="Done."
    )

    async def _create(**kwargs: Any) -> Any:
        return fake_response

    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=_create)

    handler = HandlerAbInferenceEffect(anthropic_client_factory=lambda: client)
    req = _make_request(protocol="anthropic", model_id="claude-sonnet-4-20250514")

    result = await handler.handle(req)

    assert result.error == ""
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 18
    assert result.total_tokens == 30
    assert result.raw_output == "Done."
    assert result.usage_source == EnumUsageSource.MEASURED
    assert result.correlation_id == CORRELATION_ID


@pytest.mark.asyncio
@pytest.mark.unit
async def test_anthropic_system_prompt_passed_as_kwarg() -> None:
    """System prompt is forwarded as the 'system' kwarg to client.messages.create."""
    fake_response = _make_anthropic_response()
    captured_kwargs: list[dict[str, Any]] = []

    async def _create(**kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)
        return fake_response

    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=_create)

    handler = HandlerAbInferenceEffect(anthropic_client_factory=lambda: client)
    req = _make_request(
        protocol="anthropic", system_prompt="Be concise.", prompt="Explain X."
    )

    await handler.handle(req)

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["system"] == "Be concise."
    assert captured_kwargs[0]["messages"] == [{"role": "user", "content": "Explain X."}]
    assert captured_kwargs[0]["timeout"] == 30.0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_anthropic_api_error_returns_error_result() -> None:
    """Any anthropic SDK exception produces an error result — does not raise."""

    async def _failing_create(**kwargs: Any) -> Any:
        raise RuntimeError("API rate limit exceeded")

    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=_failing_create)

    handler = HandlerAbInferenceEffect(anthropic_client_factory=lambda: client)
    req = _make_request(protocol="anthropic")

    result = await handler.handle(req)

    assert result.error != ""
    assert "RuntimeError" in result.error
    assert result.prompt_tokens == 0
    assert result.usage_source == EnumUsageSource.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.unit
async def test_anthropic_client_factory_error_returns_error_result() -> None:
    """Anthropic client construction failures stay inside the error-result boundary."""

    def _factory() -> Any:
        raise RuntimeError("missing api key")

    handler = HandlerAbInferenceEffect(anthropic_client_factory=_factory)
    req = _make_request(protocol="anthropic")

    result = await handler.handle(req)

    assert result.error != ""
    assert "RuntimeError" in result.error
    assert result.usage_source == EnumUsageSource.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.unit
async def test_anthropic_malformed_response_returns_error_result() -> None:
    """Malformed successful Anthropic responses stay inside the error-result boundary."""
    fake_response = _make_anthropic_response()
    fake_response.usage.input_tokens = object()

    client = MagicMock()
    client.messages.create = AsyncMock(return_value=fake_response)

    handler = HandlerAbInferenceEffect(anthropic_client_factory=lambda: client)
    req = _make_request(protocol="anthropic")

    result = await handler.handle(req)

    assert result.error != ""
    assert result.prompt_tokens == 0
    assert result.usage_source == EnumUsageSource.UNKNOWN


# ---------------------------------------------------------------------------
# Unsupported protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_unsupported_protocol_returns_error_result() -> None:
    """Unknown protocol produces an error result immediately."""
    handler = HandlerAbInferenceEffect()
    req = _make_request(protocol="grpc")

    result = await handler.handle(req)

    assert result.error != ""
    assert "grpc" in result.error
    assert result.usage_source == EnumUsageSource.UNKNOWN
    assert result.prompt_tokens == 0
