# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_sli_board_compute: the lab SLI history and its board (OMN-17427)."""

from omnimarket.nodes.node_lab_sli_board_compute.handlers.handler_lab_sli_board import (
    HandlerLabSliBoard,
)

__all__ = ["HandlerLabSliBoard", "NodeLabSliBoardCompute"]


class NodeLabSliBoardCompute(HandlerLabSliBoard):
    """ONEX entry-point wrapper for HandlerLabSliBoard."""
