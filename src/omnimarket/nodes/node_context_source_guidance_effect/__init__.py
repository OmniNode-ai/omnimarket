# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Guidance context source node: lane rules and architecture patterns (OMN-21111)."""

from omnimarket.nodes.node_context_source_guidance_effect.handlers.handler_context_source_guidance import (
    HandlerContextSourceGuidance,
)


class NodeContextSourceGuidanceEffect(HandlerContextSourceGuidance):
    """ONEX entry point."""


__all__ = ["HandlerContextSourceGuidance", "NodeContextSourceGuidanceEffect"]
