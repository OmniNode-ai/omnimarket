# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only work-ledger sequence gap effect."""

from omnimarket.nodes.node_work_ledger_seq_gap_effect.handlers import (
    HandlerWorkLedgerSeqGap,
)


class NodeWorkLedgerSeqGapEffect(HandlerWorkLedgerSeqGap):
    """ONEX entry point."""


__all__ = ["HandlerWorkLedgerSeqGap", "NodeWorkLedgerSeqGapEffect"]
