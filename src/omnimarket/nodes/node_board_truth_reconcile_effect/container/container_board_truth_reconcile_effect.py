# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_board_truth_compute.handlers.handler_board_truth import (
    HandlerBoardTruth,
)


def build_handler_board_truth() -> HandlerBoardTruth:
    """Build the board truth projection handler."""
    return HandlerBoardTruth()
