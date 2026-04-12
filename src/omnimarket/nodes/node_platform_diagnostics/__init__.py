# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_platform_diagnostics — Composable 7-dimension platform health check node."""

from omnimarket.nodes.node_platform_diagnostics.handlers.handler_platform_diagnostics import (
    HandlerPlatformDiagnostics,
)

__all__ = [
    "HandlerPlatformDiagnostics",
    "NodePlatformDiagnostics",
]


class NodePlatformDiagnostics(HandlerPlatformDiagnostics):
    """ONEX entry-point wrapper for HandlerPlatformDiagnostics."""
