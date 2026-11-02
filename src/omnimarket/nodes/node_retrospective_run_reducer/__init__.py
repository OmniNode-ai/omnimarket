# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_retrospective_run_reducer: scaffold from the weekly_retrospective design."""

from omnimarket.nodes.node_retrospective_run_reducer.handlers.handler_retrospective_run_reducer import (
    HandlerRetrospectiveRunReducer,
)

__all__ = ["HandlerRetrospectiveRunReducer", "NodeRetrospectiveRunReducer"]


class NodeRetrospectiveRunReducer(HandlerRetrospectiveRunReducer):
    """ONEX entry-point wrapper for HandlerRetrospectiveRunReducer."""
