# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_gap_compute.handlers.handler_gap_compute import (
    HandlerGapCompute,
)


def build_handler_gap_compute() -> HandlerGapCompute:
    """Build the handler with its constructor defaults."""
    return HandlerGapCompute()
