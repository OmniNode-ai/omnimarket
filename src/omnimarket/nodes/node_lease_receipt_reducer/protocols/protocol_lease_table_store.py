# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The store behind the reducer: the one place a kind's authoritative table lives."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.lease_receipt.model_lease_table import ModelLeaseTable


class ProtocolLeaseTableStore(Protocol):
    """Holds one authoritative table per lease kind, written by compare and swap.

    ``compare_and_write`` is the only write: it succeeds only when the held
    table's version is the one the writer read (``None`` means the writer read
    no table), so of two writers that read the same version exactly one wins.
    """

    def read(self, kind: str) -> ModelLeaseTable | None:
        """The held table of the kind, or None when no table was written yet."""
        ...

    def compare_and_write(
        self, kind: str, expected_version: int | None, table: ModelLeaseTable
    ) -> bool:
        """Write the table when the held version is ``expected_version``; else write nothing."""
        ...


__all__: list[str] = ["ProtocolLeaseTableStore"]
