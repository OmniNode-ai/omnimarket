# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerSwarmEndpointHealth."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from omnimarket.nodes.node_swarm_endpoint_health_effect.handlers.handler_swarm_endpoint_health import (
    HandlerSwarmEndpointHealth,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.models.enums import (
    EnumEndpointStatus,
    EnumModelStatus,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.models.model_swarm_endpoint import (
    ModelSwarmEndpoint,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.models.model_swarm_health_check_request import (
    ModelSwarmHealthCheckRequest,
)


def _make_endpoint(
    ep_id: str = "ep-1",
    base_url: str = "http://localhost:8000",
    model_id: str = "qwen3-coder-30b",
    health_check_path: str = "/health",
    provider: str = "vllm",
) -> ModelSwarmEndpoint:
    return ModelSwarmEndpoint(
        id=ep_id,
        base_url=base_url,
        health_check_path=health_check_path,
        model_id=model_id,
        provider=provider,
    )


def _models_body(model_ids: list[str]) -> bytes:
    data = [{"id": mid} for mid in model_ids]
    return json.dumps({"data": data}).encode()


def _make_http_get(
    responses: dict[str, tuple[int, bytes]],
) -> Callable[[str, float], Coroutine[Any, Any, tuple[int, bytes]]]:
    """Return an async http_get_fn that maps URL → (status, body)."""

    async def _get(url: str, timeout: float) -> tuple[int, bytes]:
        if url not in responses:
            raise ConnectionError(f"unexpected url: {url}")
        return responses[url]

    return _get


@pytest.mark.asyncio
async def test_healthy_endpoint_and_model_available() -> None:
    responses: dict[str, tuple[int, bytes]] = {
        "http://localhost:8000/health": (200, b"ok"),
        "http://localhost:8000/v1/models": (200, _models_body(["qwen3-coder-30b"])),
    }
    handler = HandlerSwarmEndpointHealth(http_get_fn=_make_http_get(responses))
    request = ModelSwarmHealthCheckRequest(
        endpoints=(_make_endpoint(),),
        correlation_id="test-corr-1",
    )
    result = await handler.handle(request)

    health = result.endpoint_health["ep-1"]
    assert health.endpoint_status == EnumEndpointStatus.REACHABLE
    assert health.model_status == EnumModelStatus.AVAILABLE
    assert health.error is None
    assert health.latency_ms is not None


@pytest.mark.asyncio
async def test_unreachable_endpoint() -> None:
    async def _fail(url: str, timeout: float) -> tuple[int, bytes]:
        raise ConnectionError("refused")

    handler = HandlerSwarmEndpointHealth(http_get_fn=_fail)
    request = ModelSwarmHealthCheckRequest(
        endpoints=(_make_endpoint(),),
        correlation_id="test-corr-2",
    )
    result = await handler.handle(request)

    health = result.endpoint_health["ep-1"]
    assert health.endpoint_status == EnumEndpointStatus.UNREACHABLE
    assert health.model_status == EnumModelStatus.UNKNOWN
    assert health.error is not None


@pytest.mark.asyncio
async def test_timeout_endpoint() -> None:
    import httpx

    async def _timeout(url: str, timeout: float) -> tuple[int, bytes]:
        raise httpx.TimeoutException("timed out")

    handler = HandlerSwarmEndpointHealth(http_get_fn=_timeout)
    request = ModelSwarmHealthCheckRequest(
        endpoints=(_make_endpoint(),),
        correlation_id="test-corr-3",
    )
    result = await handler.handle(request)

    health = result.endpoint_health["ep-1"]
    assert health.endpoint_status == EnumEndpointStatus.TIMEOUT
    assert health.model_status == EnumModelStatus.UNKNOWN


@pytest.mark.asyncio
async def test_model_unavailable_endpoint_healthy() -> None:
    responses: dict[str, tuple[int, bytes]] = {
        "http://localhost:8000/health": (200, b"ok"),
        "http://localhost:8000/v1/models": (200, _models_body(["other-model"])),
    }
    handler = HandlerSwarmEndpointHealth(http_get_fn=_make_http_get(responses))
    request = ModelSwarmHealthCheckRequest(
        endpoints=(_make_endpoint(),),
        correlation_id="test-corr-4",
    )
    result = await handler.handle(request)

    health = result.endpoint_health["ep-1"]
    assert health.endpoint_status == EnumEndpointStatus.REACHABLE
    assert health.model_status == EnumModelStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_auth_failed_endpoint() -> None:
    responses: dict[str, tuple[int, bytes]] = {
        "http://localhost:8000/health": (401, b"unauthorized"),
    }
    handler = HandlerSwarmEndpointHealth(http_get_fn=_make_http_get(responses))
    request = ModelSwarmHealthCheckRequest(
        endpoints=(_make_endpoint(),),
        correlation_id="test-corr-5",
    )
    result = await handler.handle(request)

    health = result.endpoint_health["ep-1"]
    assert health.endpoint_status == EnumEndpointStatus.AUTH_FAILED
    assert health.model_status == EnumModelStatus.UNKNOWN


@pytest.mark.asyncio
async def test_multiple_endpoints_mixed_health() -> None:
    responses: dict[str, tuple[int, bytes]] = {
        "http://localhost:8000/health": (200, b"ok"),
        "http://localhost:8000/v1/models": (200, _models_body(["model-a"])),
        "http://localhost:9000/health": (503, b"down"),
    }
    ep1 = _make_endpoint("ep-1", "http://localhost:8000", "model-a")
    ep2 = _make_endpoint("ep-2", "http://localhost:9000", "model-b")
    handler = HandlerSwarmEndpointHealth(http_get_fn=_make_http_get(responses))
    request = ModelSwarmHealthCheckRequest(
        endpoints=(ep1, ep2),
        correlation_id="test-corr-6",
    )
    result = await handler.handle(request)

    assert (
        result.endpoint_health["ep-1"].endpoint_status == EnumEndpointStatus.REACHABLE
    )
    assert result.endpoint_health["ep-1"].model_status == EnumModelStatus.AVAILABLE
    assert (
        result.endpoint_health["ep-2"].endpoint_status == EnumEndpointStatus.UNREACHABLE
    )
    assert result.endpoint_health["ep-2"].model_status == EnumModelStatus.UNKNOWN


@pytest.mark.asyncio
async def test_result_includes_checked_at_and_correlation() -> None:
    responses: dict[str, tuple[int, bytes]] = {
        "http://localhost:8000/health": (200, b"ok"),
        "http://localhost:8000/v1/models": (200, _models_body(["qwen3-coder-30b"])),
    }
    handler = HandlerSwarmEndpointHealth(http_get_fn=_make_http_get(responses))
    request = ModelSwarmHealthCheckRequest(
        endpoints=(_make_endpoint(),),
        correlation_id="test-corr-7",
    )
    result = await handler.handle(request)

    assert result.checked_at
    health = result.endpoint_health["ep-1"]
    assert health.checked_at
    assert health.endpoint_id == "ep-1"
