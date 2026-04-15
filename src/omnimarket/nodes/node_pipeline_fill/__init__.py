# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pipeline_fill — RSD-driven pipeline fill orchestrator."""

from omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill import (
    HandlerPipelineFill,
)

__all__ = [
    "HandlerPipelineFill",
    "NodePipelineFill",
]


class NodePipelineFill(HandlerPipelineFill):
    """ONEX entry-point wrapper for HandlerPipelineFill."""
