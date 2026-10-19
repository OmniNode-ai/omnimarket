# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Work-ledger context source node (OMN-21127)."""

from omnimarket.nodes.node_context_source_ledger_effect.handlers.handler_context_source_ledger import (
    HandlerContextSourceLedger,
)


class NodeContextSourceLedgerEffect(HandlerContextSourceLedger):
    """ONEX entry point."""


__all__ = ["HandlerContextSourceLedger", "NodeContextSourceLedgerEffect"]
