# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Interim second copy of the work ledger: the projection and the file, off the database host."""

from omnimarket.nodes.node_work_ledger_copy_effect.handlers import (
    HandlerWorkLedgerCopy,
)


class NodeWorkLedgerCopyEffect(HandlerWorkLedgerCopy):
    """ONEX entry point."""


__all__ = ["HandlerWorkLedgerCopy", "NodeWorkLedgerCopyEffect"]
