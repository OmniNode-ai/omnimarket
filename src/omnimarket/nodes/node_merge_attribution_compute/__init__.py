# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure reader of supplied merge attribution evidence."""

from .handlers.handler_merge_attribution import HandlerMergeAttribution


class NodeMergeAttributionCompute(HandlerMergeAttribution):
    """ONEX entry point."""


__all__ = ["HandlerMergeAttribution", "NodeMergeAttributionCompute"]
