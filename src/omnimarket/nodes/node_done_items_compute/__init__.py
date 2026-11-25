# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_done_items_compute: done items folded from work-ledger events (OMN-25104)."""

from omnimarket.nodes.node_done_items_compute.handlers import HandlerDoneItems

__all__ = ["HandlerDoneItems", "NodeDoneItemsCompute"]


class NodeDoneItemsCompute(HandlerDoneItems):
    """ONEX entry-point wrapper for HandlerDoneItems."""
