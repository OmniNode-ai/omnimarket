# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_retrospective_digest_render_compute: scaffold from the weekly_retrospective design."""

from omnimarket.nodes.node_retrospective_digest_render_compute.handlers.handler_retrospective_digest_render_compute import (
    HandlerRetrospectiveDigestRenderCompute,
)

__all__ = [
    "HandlerRetrospectiveDigestRenderCompute",
    "NodeRetrospectiveDigestRenderCompute",
]


class NodeRetrospectiveDigestRenderCompute(HandlerRetrospectiveDigestRenderCompute):
    """ONEX entry-point wrapper for HandlerRetrospectiveDigestRenderCompute."""
