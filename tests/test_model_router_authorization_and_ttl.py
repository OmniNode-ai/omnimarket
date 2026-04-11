# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Additional coverage: fallback authorization, health TTL, dual-degradation."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from omnibase_compat.routing.model_routing_policy import ModelRoutingPolicy

from omnimarket.nodes.node_model_router.handlers.handler_model_router import (
    HandlerModelRouter,
)
from omnimarket.nodes.node_model_router.models.model_routing_request import (
    ModelRoutingRequest,
)

_REGISTRY = {
    "qwen3-coder-30b": {
        "base_url": "http://192.168.86.201:8000",
        "health_path": "/health",
        "ci_override_url": "",
    },
    "claude-sonnet": {
        "base_url": "https://api.anthropic.com",
        "health_path": "",
        "ci_override_url": "",
    },
}


@pytest.mark.asyncio
async def test_model_router_unauthorized_role_raises_on_degradation() -> None:
    """Roles not in fallback_allowed_roles must get RuntimeError, not silent fallback."""
    policy = ModelRoutingPolicy(
        primary="qwen3-coder-30b",
        fallback="claude-sonnet",
        timeout_per_attempt_s=60.0,
        max_retries=2,
        reason_for_fallback="local timeout or unavailable",
        fallback_allowed_roles=["fixer"],
    )
    router = HandlerModelRouter(policy=policy, registry=_REGISTRY)

    with patch.object(router, "_check_health", new_callable=AsyncMock) as mock_health:
        mock_health.return_value = False
        request = ModelRoutingRequest(
            prompt="Write a function",
            role="ops",
            correlation_id="test-authz",
        )
        with pytest.raises(RuntimeError, match="not in fallback_allowed_roles"):
            await router.route_async(request)


@pytest.mark.asyncio
async def test_model_router_health_cache_expires_after_ttl() -> None:
    """Health cache entry older than 30s must trigger a fresh /health check."""
    policy = ModelRoutingPolicy(primary="qwen3-coder-30b")
    router = HandlerModelRouter(policy=policy, registry=_REGISTRY)

    router._health_cache["qwen3-coder-30b"] = (False, time.monotonic() - 31.0)

    call_count = 0

    async def fresh_check(model_key: str) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    with patch.object(router, "_check_health", side_effect=fresh_check):
        request = ModelRoutingRequest(
            prompt="Test",
            role="fixer",
            correlation_id="test-ttl",
        )
        result = await router.route_async(request)

    assert result.model_key == "qwen3-coder-30b"
    assert call_count >= 1, "Expected fresh health check after TTL expiry"


@pytest.mark.asyncio
async def test_model_router_both_degraded_raises() -> None:
    """When primary is degraded and role is unauthorized, RuntimeError must propagate."""
    policy = ModelRoutingPolicy(
        primary="qwen3-coder-30b",
        fallback="claude-sonnet",
        timeout_per_attempt_s=60.0,
        max_retries=2,
        reason_for_fallback="local timeout or unavailable",
        fallback_allowed_roles=["fixer"],
    )
    router = HandlerModelRouter(policy=policy, registry=_REGISTRY)

    with patch.object(router, "_check_health", new_callable=AsyncMock) as mock_health:
        mock_health.return_value = False
        request_unauthorized = ModelRoutingRequest(
            prompt="Write a function",
            role="ops",
            correlation_id="test-both-degraded",
        )
        with pytest.raises(RuntimeError):
            await router.route_async(request_unauthorized)


@pytest.mark.asyncio
async def test_model_router_recovery_clears_health_cache() -> None:
    """_record_success must clear health cache so next call triggers fresh check."""
    policy = ModelRoutingPolicy(primary="qwen3-coder-30b")
    router = HandlerModelRouter(policy=policy, registry=_REGISTRY)

    router._health_cache["qwen3-coder-30b"] = (False, time.monotonic())
    router._degraded.add("qwen3-coder-30b")

    router._record_success("qwen3-coder-30b")

    assert "qwen3-coder-30b" not in router._degraded
    assert "qwen3-coder-30b" not in router._health_cache


def test_model_router_missing_primary_in_registry_raises() -> None:
    """Constructor must raise ValueError when primary model_key is absent from registry."""
    policy = ModelRoutingPolicy(primary="nonexistent-model")
    with pytest.raises(ValueError, match="Registry missing required model keys"):
        HandlerModelRouter(policy=policy, registry=_REGISTRY)


def test_model_router_missing_fallback_in_registry_raises() -> None:
    """Constructor must raise ValueError when fallback model_key is absent from registry."""
    policy = ModelRoutingPolicy(
        primary="qwen3-coder-30b",
        fallback="nonexistent-fallback",
        fallback_allowed_roles=["fixer"],
        reason_for_fallback="test",
    )
    with pytest.raises(ValueError, match="Registry missing required model keys"):
        HandlerModelRouter(policy=policy, registry=_REGISTRY)
