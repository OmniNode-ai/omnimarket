# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger-rows port: the only way the handoff check reads the rolling ledger."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_handoff_check_effect.models import ModelLedgerRowsRead


class ProtocolLedgerRows(Protocol):
    """Rows and named views of the rolling ledger; never raises, an unread answer is typed."""

    def read_rows(self, window_hours: int) -> ModelLedgerRowsRead:
        """Every ledger row of the last ``window_hours`` hours, one line per row."""
        ...

    def read_view(self, view: str, lane: str | None) -> ModelLedgerRowsRead:
        """One named view of the reader node (``inbox`` for a lane, or ``holds``)."""
        ...


__all__ = ["ProtocolLedgerRows"]
