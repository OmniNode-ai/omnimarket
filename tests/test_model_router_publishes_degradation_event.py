# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""TDD target 3: router publishes ModelRoutingDegradedEvent when streak cap reached.

Failing signal: ImportError or no event emitted.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from omnibase_compat.routing.model_routing_policy import ModelRoutingPolicy
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_model_router.handlers.handler_model_router import (
    HandlerModelRouter,
)
from omnimarket.nodes.node_model_router.models.model_routing_request import (
    ModelRoutingRequest,
)
from omnimarket.nodes.node_model_router.topics import TOPIC_MODEL_ROUTING_DEGRADED

DEGRADED_TOPIC = TOPIC_MODEL_ROUTING_DEGRADED


@pytest.mark.asyncio
async def test_model_router_publishes_degradation_event() -> None:
    """ModelRoutingDegradedEvent must be published on streak cap (3 consecutive failures)."""
    policy = ModelRoutingPolicy(
        primary="qwen3-coder-30b",
        fallback="claude-sonnet",
        timeout_per_attempt_s=60.0,
        max_retries=2,
        reason_for_fallback="local timeout or unavailable",
        fallback_allowed_roles=["fixer"],
    )
    registry = {
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

    bus = EventBusInmemory(environment="test", group="omnimarket-test")
    await bus.start()

    router = HandlerModelRouter(policy=policy, registry=registry, event_bus=bus)

    with patch.object(router, "_check_health", new_callable=AsyncMock) as mock_health:
        mock_health.return_value = False
        request = ModelRoutingRequest(
            prompt="Write a function",
            role="fixer",
            correlation_id="test-corr-3",
        )
        for _ in range(3):
            await router.route_async(request)

    history = await bus.get_event_history(topic=DEGRADED_TOPIC)
    assert len(history) >= 1, (
        f"Expected degradation event on {DEGRADED_TOPIC}, got none"
    )

    payload = json.loads(history[0].value)
    assert payload["primary"] == "qwen3-coder-30b"
    assert payload["model_key"] == "qwen3-coder-30b"
    assert payload["correlation_id"] == "test-corr-3"
    assert payload["attempts"] >= 3
    assert "reason" in payload
    assert payload["elapsed_ms"] >= 0
