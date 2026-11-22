# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_topic_reaper_compute: flag stale broker topics, then name them deletable (OMN-21594)."""

from omnimarket.nodes.node_topic_reaper_compute.handlers.handler_topic_reaper import (
    HandlerTopicReaper,
)

__all__ = ["HandlerTopicReaper", "NodeTopicReaperCompute"]


class NodeTopicReaperCompute(HandlerTopicReaper):
    """ONEX entry-point wrapper for HandlerTopicReaper."""
