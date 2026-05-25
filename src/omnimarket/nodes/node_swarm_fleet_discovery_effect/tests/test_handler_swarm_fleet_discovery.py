# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerSwarmFleetDiscovery."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_swarm_fleet_discovery_effect.handlers.handler_swarm_fleet_discovery import (
    HandlerSwarmFleetDiscovery,
)
from omnimarket.nodes.node_swarm_fleet_discovery_effect.models.model_fleet_discovery_request import (
    ModelFleetDiscoveryRequest,
)
from omnimarket.nodes.node_swarm_fleet_discovery_effect.models.model_fleet_discovery_result import (
    EnumDiscoveryEndpointStatus,
)


def _make_http_get(
    responses: dict[str, tuple[int, bytes]],
) -> Callable[[str, float, dict[str, str]], Coroutine[Any, Any, tuple[int, bytes]]]:
    async def _get(
        url: str, timeout: float, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        if url not in responses:
            raise ConnectionError(f"unexpected url: {url}")
        return responses[url]

    return _get


def _openrouter_models_body(model_ids: list[str]) -> bytes:
    return json.dumps({"data": [{"id": mid} for mid in model_ids]}).encode()


_REGISTRY_PATH = (
    Path(__file__).parent.parent.parent
    / "node_swarm_registry_compute"
    / "contracts"
    / "endpoint_registry.yaml"
)

_LOCAL_HEALTH_URLS = [
    "http://192.168.86.201:8000/v1/health",  # onex-allow-internal-ip OMN-12083 reason="test fixture for fleet discovery health probe mocking"
    "http://192.168.86.201:8001/v1/health",  # onex-allow-internal-ip OMN-12083 reason="test fixture for fleet discovery health probe mocking"
    "http://192.168.86.201:8002/v1/health",  # onex-allow-internal-ip OMN-12083 reason="test fixture for fleet discovery health probe mocking"
    "http://192.168.86.200:8101/v1/health",  # onex-allow-internal-ip OMN-12083 reason="test fixture for fleet discovery health probe mocking"
]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_local_healthy_no_openrouter() -> None:
    responses = dict.fromkeys(_LOCAL_HEALTH_URLS, (200, b"ok"))
    # 5 local endpoints (including deepseek-v4-pro which shares same health URL)
    _mlx_url = "http://192.168.86.200:8101/v1/health"  # onex-allow-internal-ip OMN-12083 reason="test fixture for fleet discovery health probe mocking"
    responses[_mlx_url] = (200, b"ok")

    handler = HandlerSwarmFleetDiscovery(
        http_get_fn=_make_http_get(responses),
        registry_path=_REGISTRY_PATH,
        openrouter_api_key="",
    )
    req = ModelFleetDiscoveryRequest(
        correlation_id="test-1",
        include_local=True,
        include_openrouter=False,
        min_healthy_endpoints=4,
    )
    result = await handler.handle(req)

    assert result.local_count >= 4
    assert result.openrouter_count == 0
    assert result.meets_threshold is True
    assert result.healthy_count >= 4
    for ep in result.endpoints:
        assert ep.status == EnumDiscoveryEndpointStatus.healthy


@pytest.mark.unit
@pytest.mark.asyncio
async def test_local_unhealthy_fallback() -> None:
    # All local endpoints fail
    async def _always_fail(
        url: str, timeout: float, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        raise ConnectionError("refused")

    handler = HandlerSwarmFleetDiscovery(
        http_get_fn=_always_fail,
        registry_path=_REGISTRY_PATH,
        openrouter_api_key="",
    )
    req = ModelFleetDiscoveryRequest(
        correlation_id="test-2",
        include_local=True,
        include_openrouter=False,
        min_healthy_endpoints=4,
    )
    result = await handler.handle(req)

    assert result.healthy_count == 0
    assert result.meets_threshold is False
    for ep in result.endpoints:
        assert ep.status == EnumDiscoveryEndpointStatus.unhealthy
        assert ep.error is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openrouter_discovery_live_models() -> None:
    # OpenRouter returns several free models including ones in our catalog
    live_ids = [
        "nvidia/llama-3.1-nemotron-nano-8b-v1:free",
        "nvidia/llama-3.1-nemotron-nano-12b-v1:free",
        "featherless/qwerky-72b:free",
        "microsoft/mai-ds-r1:free",
        "openrouter/cypher-alpha:free",
        "google/gemma-3-27b-it:free",
        "qwen/qwen3-coder:free",
        "deepseek/deepseek-r1:free",
        "google/gemma-3n-e4b-it:free",
        "nvidia/llama-3.3-nemotron-super-49b-v1:free",
        "qwen/qwen3-235b-a22b:free",
        "thudm/glm-4-9b-chat:free",
    ]
    responses = {
        "https://openrouter.ai/api/v1/models": (200, _openrouter_models_body(live_ids)),
    }

    handler = HandlerSwarmFleetDiscovery(
        http_get_fn=_make_http_get(responses),
        registry_path=_REGISTRY_PATH,
        openrouter_api_key="test-key",
    )
    req = ModelFleetDiscoveryRequest(
        correlation_id="test-3",
        include_local=False,
        include_openrouter=True,
        min_healthy_endpoints=8,
    )
    result = await handler.handle(req)

    assert result.openrouter_count == 12
    assert result.healthy_count >= 8
    assert result.meets_threshold is True
    healthy_eps = [
        e for e in result.endpoints if e.status == EnumDiscoveryEndpointStatus.healthy
    ]
    assert len(healthy_eps) >= 8
    for ep in healthy_eps:
        assert ep.provider == "openrouter"
        assert ep.cost_basis == "cloud_free"
        assert ep.base_url == "https://openrouter.ai/api/v1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openrouter_api_failure_all_unhealthy() -> None:
    async def _fail(
        url: str, timeout: float, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        return 503, b"service unavailable"

    handler = HandlerSwarmFleetDiscovery(
        http_get_fn=_fail,
        registry_path=_REGISTRY_PATH,
        openrouter_api_key="test-key",
    )
    req = ModelFleetDiscoveryRequest(
        correlation_id="test-4",
        include_local=False,
        include_openrouter=True,
        min_healthy_endpoints=8,
    )
    result = await handler.handle(req)

    # All OpenRouter endpoints unhealthy when /models probe fails
    assert result.healthy_count == 0
    assert result.meets_threshold is False
    for ep in result.endpoints:
        assert ep.status == EnumDiscoveryEndpointStatus.unhealthy


@pytest.mark.unit
@pytest.mark.asyncio
async def test_combined_local_and_openrouter_meets_threshold() -> None:
    # 4 healthy local + 8 healthy openrouter = 12 >= threshold of 8
    live_ids = [
        "nvidia/llama-3.1-nemotron-nano-8b-v1:free",
        "nvidia/llama-3.1-nemotron-nano-12b-v1:free",
        "featherless/qwerky-72b:free",
        "microsoft/mai-ds-r1:free",
        "openrouter/cypher-alpha:free",
        "google/gemma-3-27b-it:free",
        "qwen/qwen3-coder:free",
        "deepseek/deepseek-r1:free",
    ]
    responses: dict[str, tuple[int, bytes]] = dict.fromkeys(
        _LOCAL_HEALTH_URLS, (200, b"ok")
    )
    responses["https://openrouter.ai/api/v1/models"] = (
        200,
        _openrouter_models_body(live_ids),
    )

    handler = HandlerSwarmFleetDiscovery(
        http_get_fn=_make_http_get(responses),
        registry_path=_REGISTRY_PATH,
        openrouter_api_key="test-key",
    )
    req = ModelFleetDiscoveryRequest(
        correlation_id="test-5",
        include_local=True,
        include_openrouter=True,
        min_healthy_endpoints=8,
    )
    result = await handler.handle(req)

    assert result.meets_threshold is True
    assert result.healthy_count >= 8
    assert result.local_count >= 4
    assert result.openrouter_count == 12


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openrouter_partial_availability() -> None:
    # Only 3 of our catalog models are live — below threshold of 8
    live_ids = [
        "nvidia/llama-3.1-nemotron-nano-8b-v1:free",
        "qwen/qwen3-coder:free",
        "deepseek/deepseek-r1:free",
    ]
    responses = {
        "https://openrouter.ai/api/v1/models": (200, _openrouter_models_body(live_ids)),
    }

    handler = HandlerSwarmFleetDiscovery(
        http_get_fn=_make_http_get(responses),
        registry_path=_REGISTRY_PATH,
        openrouter_api_key="test-key",
    )
    req = ModelFleetDiscoveryRequest(
        correlation_id="test-6",
        include_local=False,
        include_openrouter=True,
        min_healthy_endpoints=8,
    )
    result = await handler.handle(req)

    healthy = [
        e for e in result.endpoints if e.status == EnumDiscoveryEndpointStatus.healthy
    ]
    assert len(healthy) == 3
    assert result.meets_threshold is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_result_fields_populated() -> None:
    live_ids = ["nvidia/llama-3.1-nemotron-nano-8b-v1:free"]
    responses = {
        "https://openrouter.ai/api/v1/models": (200, _openrouter_models_body(live_ids)),
    }

    handler = HandlerSwarmFleetDiscovery(
        http_get_fn=_make_http_get(responses),
        registry_path=_REGISTRY_PATH,
        openrouter_api_key="key",
    )
    req = ModelFleetDiscoveryRequest(
        correlation_id="test-7",
        run_id="run-abc",
        include_local=False,
        include_openrouter=True,
        min_healthy_endpoints=1,
    )
    result = await handler.handle(req)

    assert result.run_id == "run-abc"
    assert result.correlation_id == "test-7"
    assert result.discovered_at != ""
    assert result.meets_threshold is True
    ep = next(
        e for e in result.endpoints if e.status == EnumDiscoveryEndpointStatus.healthy
    )
    assert ep.model_id == "nvidia/llama-3.1-nemotron-nano-8b-v1:free"
    assert "general" in ep.capabilities
