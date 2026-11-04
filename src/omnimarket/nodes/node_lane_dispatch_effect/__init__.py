# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public bus ingress for checked, serialized lane dispatch."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from omnimarket.models.lane_dispatch.model_lane_dispatch_effect import (
    ModelLaneDispatchRequest,
    ModelLaneDispatchResult,
)


def dispatch(request: ModelLaneDispatchRequest) -> ModelLaneDispatchResult:
    """Submit to the declared node over its deployment-bound runtime bus."""
    from .runtime_lane_dispatch_cli import dispatch as dispatch_over_bus

    # Synchronous canonical handlers can themselves be called by RuntimeLocal's
    # active event loop. Run the nested runtime on one worker, preserving the
    # caller's transport context, rather than nesting asyncio.run on that loop.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return dispatch_over_bus(request)
    context = copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(context.run, dispatch_over_bus, request).result()
