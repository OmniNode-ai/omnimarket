# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Work ledger graph compute node: the graph from the rows' own cells, no model call."""

from omnimarket.nodes.node_work_ledger_graph_compute.handlers.handler_work_ledger_graph import (
    HandlerWorkLedgerGraph,
)


class NodeWorkLedgerGraphCompute(HandlerWorkLedgerGraph):
    """ONEX entry point."""


__all__ = ["HandlerWorkLedgerGraph", "NodeWorkLedgerGraphCompute"]
