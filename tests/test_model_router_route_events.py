# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-11927 reason="test fixture uses lab LLM endpoints to verify route event payloads"

"""Tests for canonical node_model_router route resolved/rejected events."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from omnibase_core.enums.enum_routing_error_class import RoutingErrorClass
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.routing.model_routing_policy import ModelRoutingPolicy

from omnimarket.nodes.node_model_router.handlers.handler_model_router import (
    TOPIC_MODEL_LLM_ROUTE_REJECTED,
    TOPIC_MODEL_LLM_ROUTE_RESOLVED,
    HandlerModelRouter,
)
from omnimarket.nodes.node_model_router.models.model_routing_request import (
    ModelRoutingRequest,
)

_REGISTRY = {
    "qwen3-coder-30b": {
        "base_url": "http://localhost:8000",
        "health_path": "/health",
        "ci_override_url": "",
        "served_model_id": "qwen/qwen3-coder-30b",
        "endpoint_ref": "LLM_LOCAL_PRIMARY_URL",
        "provider": "local",
        "pricing_manifest_hash": "sha256:pricing",
    },
    "claude-sonnet": {
        "base_url": "https://api.anthropic.com",
        "health_path": "",
        "ci_override_url": "",
        "served_model_id": "claude-sonnet-4",
        "endpoint_ref": "ANTHROPIC_API_KEY",
        "provider": "anthropic",
        "pricing_manifest_hash": "sha256:pricing",
    },
}


@pytest.mark.asyncio
async def test_model_router_publishes_route_resolved_event() -> None:
    policy = ModelRoutingPolicy(primary="qwen3-coder-30b")
    bus = EventBusInmemory(environment="test", group="omnimarket-test")
    await bus.start()
    router = HandlerModelRouter(policy=policy, registry=_REGISTRY, event_bus=bus)

    with patch.object(router, "_check_health", new_callable=AsyncMock) as mock_health:
        mock_health.return_value = True
        request = ModelRoutingRequest(
            prompt="Write a function",
            role="fixer",
            correlation_id="test-route-resolved",
        )
        await router.route_async(request)

    history = await bus.get_event_history(topic=TOPIC_MODEL_LLM_ROUTE_RESOLVED)
    assert len(history) == 1
    payload = json.loads(history[0].value)
    assert payload["logical_model_key"] == "qwen3-coder-30b"
    assert payload["served_model_id"] == "qwen/qwen3-coder-30b"
    assert payload["endpoint_ref"] == "LLM_LOCAL_PRIMARY_URL"
    assert payload["provider"] == "local"
    assert payload["policy_hash"] == payload["routing_policy_hash"]
    assert payload["pricing_manifest_hash"] == "sha256:pricing"


@pytest.mark.asyncio
async def test_model_router_publishes_route_rejected_event() -> None:
    policy = ModelRoutingPolicy(
        primary="qwen3-coder-30b",
        fallback="claude-sonnet",
        reason_for_fallback="local timeout or unavailable",
        fallback_allowed_roles=["fixer"],
    )
    bus = EventBusInmemory(environment="test", group="omnimarket-test")
    await bus.start()
    router = HandlerModelRouter(policy=policy, registry=_REGISTRY, event_bus=bus)

    with patch.object(router, "_check_health", new_callable=AsyncMock) as mock_health:
        mock_health.return_value = False
        request = ModelRoutingRequest(
            prompt="Write a function",
            role="ops",
            correlation_id="test-route-rejected",
        )
        with pytest.raises(RuntimeError, match="not in fallback_allowed_roles"):
            await router.route_async(request)

    history = await bus.get_event_history(topic=TOPIC_MODEL_LLM_ROUTE_REJECTED)
    assert len(history) == 1
    payload = json.loads(history[0].value)
    assert payload["logical_model_key"] == "qwen3-coder-30b"
    assert payload["failure_class"] == RoutingErrorClass.FALLBACK_UNAUTHORIZED.value
    assert payload["fallback_reason"] == "local timeout or unavailable"
    assert payload["policy_hash"] == payload["routing_policy_hash"]
