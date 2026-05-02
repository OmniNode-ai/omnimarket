# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler that calls an LLM endpoint and returns measured token usage.

Supports two protocols:
  - openai_compatible: POST to {endpoint_url}/v1/chat/completions via httpx.
  - anthropic: uses the anthropic SDK Messages API (import-guarded).

On timeout or connection error the result carries a non-empty error field and
zero token counts — it never raises.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Coroutine
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_ab_inference_effect.models.model_inference_request import (
    ModelInferenceRequest,
)
from omnimarket.nodes.node_ab_inference_effect.models.model_inference_result import (
    ModelInferenceResult,
)

logger = logging.getLogger(__name__)

# Type alias for an injectable async HTTP post callable
_HttpPostFn = Callable[
    [str, dict[str, Any], dict[str, str], float],
    Coroutine[Any, Any, tuple[int, dict[str, Any]]],
]

_ENDPOINT_ALLOWLIST_ENVS = (
    "LLM_CODER_URL",
    "LLM_CODER_FAST_URL",
    "LLM_DEEPSEEK_R1_URL",
    "LLM_QWEN3_NEXT_URL",
    "LLM_GLM_URL",
)


async def _default_http_post(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    """Default httpx POST — returns (status_code, response_json)."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.status_code, resp.json()


class HandlerAbInferenceEffect:
    """Calls an LLM endpoint and returns measured token counts + latency.

    Inject ``http_post_fn`` in tests to avoid real network calls.
    Inject ``anthropic_client_factory`` to replace the anthropic SDK in tests.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        http_post_fn: _HttpPostFn | None = None,
        anthropic_client_factory: Callable[[], Any] | None = None,
        allowed_endpoint_urls: set[str] | None = None,
    ) -> None:
        self._http_post = http_post_fn or _default_http_post
        self._anthropic_client_factory = anthropic_client_factory
        self._allowed_endpoint_urls = (
            allowed_endpoint_urls
            if allowed_endpoint_urls is not None
            else _configured_endpoint_allowlist()
        )

    async def handle(self, request: ModelInferenceRequest) -> ModelInferenceResult:
        """Dispatch to the appropriate protocol handler.

        Args:
            request: Fully-resolved inference request from the orchestrator.

        Returns:
            ModelInferenceResult with measured token counts, latency, and raw output.
            On error: error field is non-empty, token counts are zero.
        """
        logger.info(
            "ab-inference started (model_key=%s, protocol=%s, correlation_id=%s)",
            request.model_key,
            request.protocol,
            request.correlation_id,
        )

        if request.protocol == "openai_compatible":
            return await self._call_openai_compatible(request)
        if request.protocol == "anthropic":
            return await self._call_anthropic(request)

        return ModelInferenceResult(
            model_key=request.model_key,
            correlation_id=request.correlation_id,
            error=f"Unsupported protocol: {request.protocol!r}",
            usage_source=EnumUsageSource.UNKNOWN,
        )

    async def _call_openai_compatible(
        self, request: ModelInferenceRequest
    ) -> ModelInferenceResult:
        if not _is_endpoint_allowed(request.endpoint_url, self._allowed_endpoint_urls):
            return self._error_result(
                request,
                f"Endpoint URL is not in the configured allowlist: {request.endpoint_url}",
            )

        url = f"{request.endpoint_url.rstrip('/')}/v1/chat/completions"
        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        payload: dict[str, Any] = {
            "model": request.model_id,
            "messages": messages,
        }
        headers = {"Content-Type": "application/json"}

        t0 = time.monotonic()
        try:
            _status, data = await self._http_post(
                url, payload, headers, request.timeout_seconds
            )
            return self._parse_openai_response(request, data, t0)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "ab-inference timeout/connect error (model_key=%s): %s",
                request.model_key,
                exc,
            )
            return ModelInferenceResult(
                model_key=request.model_key,
                correlation_id=request.correlation_id,
                latency_ms=latency_ms,
                error=f"{type(exc).__name__}: {exc}",
                usage_source=EnumUsageSource.UNKNOWN,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "ab-inference HTTP error (model_key=%s): %s",
                request.model_key,
                exc,
            )
            return ModelInferenceResult(
                model_key=request.model_key,
                correlation_id=request.correlation_id,
                latency_ms=latency_ms,
                error=f"{type(exc).__name__}: {exc}",
                usage_source=EnumUsageSource.UNKNOWN,
            )

    def _parse_openai_response(
        self, request: ModelInferenceRequest, data: dict[str, Any], t0: float
    ) -> ModelInferenceResult:
        latency_ms = int((time.monotonic() - t0) * 1000)

        usage = data.get("usage") or {}
        prompt_tokens: int = int(usage.get("prompt_tokens", 0))
        completion_tokens: int = int(usage.get("completion_tokens", 0))
        total_tokens: int = int(
            usage.get("total_tokens", prompt_tokens + completion_tokens)
        )

        raw_output = ""
        choices = data.get("choices") or []
        if choices:
            raw_output = str((choices[0].get("message") or {}).get("content", ""))

        usage_source = EnumUsageSource.MEASURED if usage else EnumUsageSource.UNKNOWN

        logger.info(
            "ab-inference complete (model_key=%s, prompt_tokens=%d, completion_tokens=%d, latency_ms=%d)",
            request.model_key,
            prompt_tokens,
            completion_tokens,
            latency_ms,
        )

        return ModelInferenceResult(
            model_key=request.model_key,
            correlation_id=request.correlation_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            raw_output=raw_output,
            usage_source=usage_source,
        )

    async def _call_anthropic(
        self, request: ModelInferenceRequest
    ) -> ModelInferenceResult:
        if self._anthropic_client_factory is not None:
            try:
                client = self._anthropic_client_factory()
            except Exception as exc:
                return self._error_result(request, f"{type(exc).__name__}: {exc}")
        else:
            try:
                import anthropic as _anthropic
            except ImportError:
                return ModelInferenceResult(
                    model_key=request.model_key,
                    correlation_id=request.correlation_id,
                    error="anthropic SDK not installed; add anthropic to dependencies",
                    usage_source=EnumUsageSource.UNKNOWN,
                )
            try:
                client = _anthropic.AsyncAnthropic()
            except Exception as exc:
                return self._error_result(request, f"{type(exc).__name__}: {exc}")

        messages: list[dict[str, str]] = [{"role": "user", "content": request.prompt}]

        kwargs: dict[str, Any] = {
            "model": request.model_id,
            "max_tokens": 4096,
            "messages": messages,
        }
        if request.system_prompt:
            kwargs["system"] = request.system_prompt

        t0 = time.monotonic()
        try:
            response = await client.messages.create(
                **kwargs, timeout=request.timeout_seconds
            )
            return self._parse_anthropic_response(request, response, t0)
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "ab-inference anthropic error (model_key=%s): %s",
                request.model_key,
                exc,
            )
            return ModelInferenceResult(
                model_key=request.model_key,
                correlation_id=request.correlation_id,
                latency_ms=latency_ms,
                error=f"{type(exc).__name__}: {exc}",
                usage_source=EnumUsageSource.UNKNOWN,
            )

    def _parse_anthropic_response(
        self, request: ModelInferenceRequest, response: Any, t0: float
    ) -> ModelInferenceResult:
        latency_ms = int((time.monotonic() - t0) * 1000)

        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "input_tokens", 0))
        completion_tokens = int(getattr(usage, "output_tokens", 0))
        total_tokens = prompt_tokens + completion_tokens

        raw_output = ""
        content_blocks = getattr(response, "content", []) or []
        if content_blocks:
            raw_output = str(getattr(content_blocks[0], "text", ""))

        usage_source = (
            EnumUsageSource.MEASURED if usage is not None else EnumUsageSource.UNKNOWN
        )

        logger.info(
            "ab-inference anthropic complete (model_key=%s, prompt_tokens=%d, completion_tokens=%d, latency_ms=%d)",
            request.model_key,
            prompt_tokens,
            completion_tokens,
            latency_ms,
        )

        return ModelInferenceResult(
            model_key=request.model_key,
            correlation_id=request.correlation_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            raw_output=raw_output,
            usage_source=usage_source,
        )

    def _error_result(
        self, request: ModelInferenceRequest, error: str
    ) -> ModelInferenceResult:
        return ModelInferenceResult(
            model_key=request.model_key,
            correlation_id=request.correlation_id,
            error=error,
            usage_source=EnumUsageSource.UNKNOWN,
        )


def _configured_endpoint_allowlist() -> set[str]:
    return {
        normalized
        for value in (os.environ.get(name, "") for name in _ENDPOINT_ALLOWLIST_ENVS)
        if (normalized := _normalize_endpoint_url(value))
    }


def _is_endpoint_allowed(endpoint_url: str, allowed_endpoint_urls: set[str]) -> bool:
    normalized = _normalize_endpoint_url(endpoint_url)
    return bool(normalized and normalized in allowed_endpoint_urls)


def _normalize_endpoint_url(endpoint_url: str) -> str:
    parsed = urlparse(endpoint_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname.lower()}{port}"


__all__: list[str] = ["HandlerAbInferenceEffect"]
