# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probe listed models through the same bounded transport as delegation."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from omnimarket.handlers.routing.byok_model_discovery import models_request_headers
from omnimarket.inference.provider_response_error import provider_message_from_text
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
    ModelTransportResponse,
    post_chat_completion,
)
from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_probe import (
    ModelEndpointProbe,
    ModelEndpointProbeRequest,
    ModelEndpointProbeResult,
)


class HandlerEndpointProbe:
    """Availability is distinct from the subsequent real delegation proof."""

    def __init__(
        self,
        *,
        post: Callable[..., ModelTransportResponse] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._post = post or post_chat_completion
        self._clock = clock

    def handle(self, request: ModelEndpointProbeRequest) -> ModelEndpointProbeResult:
        deadline = self._clock() + request.total_timeout_seconds
        return ModelEndpointProbeResult(
            url=request.url,
            probes=tuple(
                self._probe(request, model, deadline) for model in request.models
            ),
        )

    def _probe(
        self, request: ModelEndpointProbeRequest, model: str, deadline: float
    ) -> ModelEndpointProbe:
        secret = request.key.get_secret_value() if request.key is not None else None
        backend = request.backend
        headers = (
            models_request_headers(backend, secret or "")
            if backend
            else ({"Authorization": "Bearer " + secret} if secret else {})
        )
        native = (
            backend is not None and backend.inference_protocol == "anthropic_messages"
        )
        status = None
        message = ""
        for parameter in (
            (backend.token_limit_field,)
            if backend
            else ("max_completion_tokens", "max_tokens")
        ):
            remaining = deadline - self._clock()
            if remaining <= 0:
                return ModelEndpointProbe(
                    model=model,
                    status="unprobed",
                    message="Model availability probe budget expired before this request; this model is listed but not verified.",
                )
            try:
                response = self._post(
                    endpoint_url=backend.endpoint_url
                    if backend
                    else request.url + "/v1/chat/completions",
                    payload={
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        parameter: 1,
                        "stream": False,
                    },
                    timeout_seconds=min(request.timeout_seconds, remaining),
                    extra_headers=headers,
                )
                status = response.status_code
                body: Any = response.json_body
                if (
                    status == 200
                    and isinstance(body, dict)
                    and body.get("content" if native else "choices")
                    and not body.get("error")
                ):
                    return ModelEndpointProbe(
                        model=model, status="ready", http_status=status
                    )
                message = "The endpoint did not return a successful chat completion."
                break
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                message = (
                    provider_message_from_text(exc.response.text)
                    or "The provider refused this availability probe."
                )
                if secret:
                    message = message.replace(secret, "[redacted]")
                message = " ".join(message.split())[:300]
                if (
                    status == 400
                    and parameter == "max_completion_tokens"
                    and "max_tokens" in message
                ):
                    continue
                if status == 429:
                    return ModelEndpointProbe(
                        model=model,
                        status="throttled",
                        http_status=status,
                        message=message,
                    )
                break
            except (httpx.HTTPError, OSError, RuntimeError, ValueError):
                message = "The model did not answer the bounded availability probe."
                break
        return ModelEndpointProbe(
            model=model, status="unavailable", http_status=status, message=message
        )
