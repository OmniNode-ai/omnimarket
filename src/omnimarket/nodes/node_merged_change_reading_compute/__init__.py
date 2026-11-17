# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merged-change reading compute node."""

from omnimarket.nodes.node_merged_change_reading_compute.handlers.handler_merged_change_reading import (
    HandlerMergedChangeReading,
)


class NodeMergedChangeReadingCompute(HandlerMergedChangeReading):
    """ONEX entrypoint for the per-change reading of merged work against the running system."""


__all__ = ["HandlerMergedChangeReading", "NodeMergedChangeReadingCompute"]
