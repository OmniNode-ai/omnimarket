# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merged-and-running count compute node."""

from omnimarket.nodes.node_merged_running_count_compute.handlers.handler_merged_running_count import (
    HandlerMergedRunningCount,
)


class NodeMergedRunningCountCompute(HandlerMergedRunningCount):
    """ONEX entrypoint for the merged-and-running count and the idle slots."""


__all__ = ["HandlerMergedRunningCount", "NodeMergedRunningCountCompute"]
