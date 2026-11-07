# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ledger reader moved off the file: it answers from the bus-fed query node, or not at all."""

from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)


class NodeWorkLedgerReaderEffect(HandlerWorkLedgerReader):
    """ONEX entry point."""


__all__ = ["HandlerWorkLedgerReader", "NodeWorkLedgerReaderEffect"]
