# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler factories for node_skill_dispatch_engine_orchestrator."""

from __future__ import annotations

from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.container.container_dispatch_dependencies import (
    HandlerRsdFill,
    create_handler_rsd_fill,
    create_handler_self_healing_dispatch_orchestrator,
)
from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.handlers.handler_dispatch_router import (
    HandlerDispatchEngineRouter,
)

__all__ = [
    "HandlerRsdFill",
    "create_handler_dispatch_engine_router",
    "create_handler_rsd_fill",
    "create_handler_self_healing_dispatch_orchestrator",
]


def create_handler_dispatch_engine_router() -> HandlerDispatchEngineRouter:
    """Build the dispatch router with its default dependencies."""
    return HandlerDispatchEngineRouter()
