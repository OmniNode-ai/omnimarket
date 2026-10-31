# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind the dispatch router's collaborators without importing the router."""

from __future__ import annotations

from omnimarket.nodes.node_rsd_fill_compute.handlers.handler_rsd_fill import (
    HandlerRsdFill,
)
from omnimarket.nodes.node_self_healing_dispatch_orchestrator.handlers.handler_self_healing_dispatch_orchestrator import (
    HandlerSelfHealingDispatchOrchestrator,
    ProtocolSelfHealingDispatcher,
)

__all__ = [
    "HandlerRsdFill",
    "create_handler_rsd_fill",
    "create_handler_self_healing_dispatch_orchestrator",
]


def create_handler_rsd_fill() -> HandlerRsdFill:
    """Build the RSD scoring handler."""
    return HandlerRsdFill()


def create_handler_self_healing_dispatch_orchestrator(
    *, dispatcher: ProtocolSelfHealingDispatcher | None
) -> HandlerSelfHealingDispatchOrchestrator:
    """Build the self-healing handler with the selected dispatcher."""
    return HandlerSelfHealingDispatchOrchestrator(dispatcher=dispatcher)
