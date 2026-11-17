# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Work ledger graph query effect: five typed questions over the ledger graph."""

from omnimarket.nodes.node_work_ledger_graph_query_effect.handlers.handler_work_ledger_graph_query import (
    HandlerWorkLedgerGraphQuery,
)


class NodeWorkLedgerGraphQueryEffect(HandlerWorkLedgerGraphQuery):
    """ONEX entry point."""


__all__ = ["HandlerWorkLedgerGraphQuery", "NodeWorkLedgerGraphQueryEffect"]
