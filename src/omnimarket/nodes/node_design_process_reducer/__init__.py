# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_design_process_reducer: the design record lifecycle (OMN-21460)."""

from omnimarket.nodes.node_design_process_reducer.handlers.handler_design_process import (
    HandlerDesignProcess,
)

__all__ = ["HandlerDesignProcess", "NodeDesignProcessReducer"]


class NodeDesignProcessReducer(HandlerDesignProcess):
    """ONEX entry-point wrapper for HandlerDesignProcess."""
