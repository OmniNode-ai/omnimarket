# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local ONEX wrapper for node_brief_compact_compute."""

from omnimarket.nodes.node_brief_compact_compute.handlers.handler_brief_compact import (
    HandlerBriefCompact,
)


class NodeBriefCompactCompute(HandlerBriefCompact):
    """ONEX entry-point wrapper for HandlerBriefCompact."""


__all__ = ["HandlerBriefCompact", "NodeBriefCompactCompute"]
