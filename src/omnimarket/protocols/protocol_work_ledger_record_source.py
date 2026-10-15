# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger of record, read by seq: the repair source and the head probe (OMN-20275).

The ledger of record is the serve process's ledger, never a projection's own maximum
seq: a projection that lost its tail cannot see the loss in its own maximum.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.model_work_ledger_render_row import ModelWorkLedgerRenderRow


class WorkLedgerSourceError(Exception):
    """The ledger of record could not answer (unreachable, timed out, refused)."""


class ProtocolWorkLedgerRecordSource(Protocol):
    def read_row(
        self, *, ledger_id: str, ledger_seq: int
    ) -> ModelWorkLedgerRenderRow | None:
        """The row at ``ledger_seq``, or None when the source has no such row."""
        ...

    def read_head(self, *, ledger_id: str) -> int:
        """The highest ``ledger_seq`` the ledger of record holds (0 when empty)."""
        ...


__all__: list[str] = ["ProtocolWorkLedgerRecordSource", "WorkLedgerSourceError"]
