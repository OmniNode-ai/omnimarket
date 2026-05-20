# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pipeline_fill — RSD-driven pipeline fill effect."""

from omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill import (
    HandlerPipelineFill,
)

__all__ = [
    "HandlerPipelineFill",
    "NodePipelineFill",
]


class NodePipelineFill(HandlerPipelineFill):
    """ONEX effect entry-point wrapper for HandlerPipelineFill."""
