# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger append port of node_pr_handoff_ledger_effect (OMN-20636)."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_work_ledger_append_effect.models import (
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)


class ProtocolPrHandoffLedgerAppender(Protocol):
    """Send one ledger append request and wait for its receipt.

    Returns None when no receipt arrived within ``timeout_s``: the outcome is
    unknown, and resending the same request id is safe (the ledger host
    deduplicates on it).
    """

    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None: ...


__all__ = ["ProtocolPrHandoffLedgerAppender"]
