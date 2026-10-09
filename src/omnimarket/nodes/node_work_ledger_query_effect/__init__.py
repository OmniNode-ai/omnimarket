# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Database work ledger read node (OMN-20738)."""

from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)


class NodeWorkLedgerQueryEffect(HandlerWorkLedgerQuery):
    """ONEX entry point."""


__all__ = ["HandlerWorkLedgerQuery", "NodeWorkLedgerQueryEffect"]
