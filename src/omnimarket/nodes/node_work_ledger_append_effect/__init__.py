# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger-host append effect, reached over the bus (OMN-20275)."""

from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers import (
    HandlerWorkLedgerAppendEffect,
)


class NodeWorkLedgerAppendEffect(HandlerWorkLedgerAppendEffect):
    """ONEX entry-point wrapper for HandlerWorkLedgerAppendEffect."""


__all__ = [
    "EnumWorkLedgerAppendStatus",
    "HandlerWorkLedgerAppendEffect",
    "ModelWorkLedgerAppendReceipt",
    "ModelWorkLedgerAppendRequest",
    "NodeWorkLedgerAppendEffect",
]
