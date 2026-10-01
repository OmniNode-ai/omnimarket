# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger-host append effect, reached over the bus (OMN-20275)."""

from omnimarket.nodes.node_work_ledger_append_effect.handlers import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.models import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
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
