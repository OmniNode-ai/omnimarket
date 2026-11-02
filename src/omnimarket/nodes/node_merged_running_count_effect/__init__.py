# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merged-and-running count reader effect node."""

from omnimarket.nodes.node_merged_running_count_effect.handlers.handler_merged_running_count_effect import (
    HandlerMergedRunningCountEffect,
)


class NodeMergedRunningCountEffect(HandlerMergedRunningCountEffect):
    """ONEX entrypoint: read the count's sources through the container and call the compute node."""


__all__ = ["HandlerMergedRunningCountEffect", "NodeMergedRunningCountEffect"]
