# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""TDD target 1: router selects local primary when health endpoint is healthy.

Failing signal: ImportError or AttributeError — node_model_router module does not exist yet.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from omnibase_compat.routing.model_routing_policy import ModelRoutingPolicy

from omnimarket.nodes.node_model_router.handlers.handler_model_router import (
    HandlerModelRouter,
)
from omnimarket.nodes.node_model_router.models.model_routing_request import (
    ModelRoutingRequest,
)


@pytest.mark.asyncio
async def test_model_router_routes_to_local_first() -> None:
    """Router must select the primary local model when its /health returns 200."""
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

    router = HandlerModelRouter(policy=policy, registry=registry)

    with patch.object(router, "_check_health", new_callable=AsyncMock) as mock_health:
        mock_health.return_value = True
        request = ModelRoutingRequest(
            prompt="Write a function",
            role="fixer",
            correlation_id="test-corr-1",
        )
        result = await router.route_async(request)

    assert result.model_key == "qwen3-coder-30b"
    assert "192.168.86.201:8000" in result.endpoint_url
