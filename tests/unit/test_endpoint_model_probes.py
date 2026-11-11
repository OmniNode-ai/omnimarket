# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probe transport failure controls: token dialect, bounded work and key redaction."""

import httpx
from pydantic import SecretStr

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
    ModelTransportResponse,
)


def test_token_parameter_retry_and_provider_reason_redaction() -> None:
    from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_probe import (
        HandlerEndpointProbe,
    )
    from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_probe import (
        ModelEndpointProbeRequest,
    )

    calls = []
    secret = "private-sentinel-without-provider-prefix"

    def post(**kwargs):
        calls.append(kwargs)
        model = kwargs["payload"]["model"]
        status = 404 if model == "retired" else 400
        message = (
            "model retired " + secret
            if model == "retired"
            else "use max_tokens instead"
        )
        if model == "ready" and "max_tokens" in kwargs["payload"]:
            return ModelTransportResponse(
                status_code=200,
                json_body={"choices": [{"message": {"content": "ok"}}]},
                latency_ms=1,
            )
        response = httpx.Response(
            status,
            json={"error": {"message": message}},
            request=httpx.Request("POST", kwargs["endpoint_url"]),
        )
        raise httpx.HTTPStatusError(
            "fixture", request=response.request, response=response
        )

    result = HandlerEndpointProbe(post=post).handle(
        ModelEndpointProbeRequest(
            url="https://example.test",
            models=("ready", "retired"),
            key=SecretStr(secret),
        )
    )
    assert result.models == ("ready",)
    assert ["max_tokens" in call["payload"] for call in calls] == [False, True, False]
    assert {call["extra_headers"]["Authorization"] for call in calls} == {
        "Bearer " + secret
    }
    assert secret not in result.model_dump_json()
    assert "model retired" in result.probes[-1].message


def test_expired_batch_budget_does_not_launch_remaining_models() -> None:
    from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_probe import (
        HandlerEndpointProbe,
    )
    from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_probe import (
        ModelEndpointProbeRequest,
    )

    now = [0.0]
    called = []

    def post(**kwargs):
        called.append(kwargs["payload"]["model"])
        now[0] = 2.0
        return ModelTransportResponse(
            status_code=200,
            json_body={"choices": [{"message": {"content": "ok"}}]},
            latency_ms=1,
        )

    result = HandlerEndpointProbe(post=post, clock=lambda: now[0]).handle(
        ModelEndpointProbeRequest(
            url="http://example.test",
            models=("first", "remaining"),
            total_timeout_seconds=1,
        )
    )
    assert called == ["first"]
    assert result.models == ("first", "remaining")
    assert result.probes[-1].status == "unprobed"
    assert result.all_throttled is False
    assert "budget" in result.probes[-1].message
