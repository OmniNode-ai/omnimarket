# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""TDD target 5: retry delays follow min(1 * 2^attempt, 30s) ± 20% jitter.

Failing signal: ImportError or fixed/no sleep between retries.
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest
from omnibase_compat.routing.model_routing_policy import ModelRoutingPolicy

from omnimarket.nodes.node_model_router.handlers.handler_model_router import (
    HandlerModelRouter,
)
from omnimarket.nodes.node_model_router.models.model_routing_result import (
    ModelRoutingResult,
)


@pytest.mark.asyncio
async def test_model_router_exponential_backoff_between_retries() -> None:
    """Inter-retry delays must follow min(1 * 2^attempt, 30s) ± 20% jitter.

    Attempt 0→1: base=1s, range=[0.8, 1.2]
    Attempt 1→2: base=2s, range=[1.6, 2.4]
    """
    policy = ModelRoutingPolicy(
        primary="qwen3-coder-30b",
        fallback="claude-sonnet",
        timeout_per_attempt_s=60.0,
        max_retries=3,
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

    sleep_calls: list[float] = []

    async def capture_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    call_count = 0

    async def failing_work() -> ModelRoutingResult:
        nonlocal call_count
        call_count += 1
        raise RuntimeError(f"transient failure #{call_count}")

    with (
        patch(
            "omnimarket.nodes.node_model_router.handlers.handler_model_router.asyncio.sleep",
            side_effect=capture_sleep,
        ),
        contextlib.suppress(RuntimeError),
    ):
        await router.execute_with_retries(failing_work)

    assert len(sleep_calls) >= 2, f"Expected at least 2 sleep calls, got {sleep_calls}"

    delay_0 = sleep_calls[0]
    delay_1 = sleep_calls[1]

    assert 0.8 <= delay_0 <= 1.2, f"Attempt 0→1 delay {delay_0} not in [0.8, 1.2]"
    assert 1.6 <= delay_1 <= 2.4, f"Attempt 1→2 delay {delay_1} not in [1.6, 2.4]"
    assert call_count == 3, f"Expected 3 attempts, got {call_count}"
