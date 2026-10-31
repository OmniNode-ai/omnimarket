# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_platform_diagnostics.handlers.handler_platform_diagnostics import (
    HandlerPlatformDiagnostics,
)


def build_handler_platform_diagnostics() -> HandlerPlatformDiagnostics:
    """Build the handler with its constructor defaults."""
    return HandlerPlatformDiagnostics()
