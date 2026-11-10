# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_retrospective_run_projection_effect: scaffold from the weekly_retrospective design."""

from omnimarket.nodes.node_retrospective_run_projection_effect.handlers.handler_retrospective_run_projection_effect import (
    HandlerRetrospectiveRunProjectionEffect,
)

__all__ = [
    "HandlerRetrospectiveRunProjectionEffect",
    "NodeRetrospectiveRunProjectionEffect",
]


class NodeRetrospectiveRunProjectionEffect(HandlerRetrospectiveRunProjectionEffect):
    """ONEX entry-point wrapper for HandlerRetrospectiveRunProjectionEffect."""
