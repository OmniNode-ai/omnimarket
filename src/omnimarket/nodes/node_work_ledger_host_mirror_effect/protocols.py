# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam the host mirror reads the ledger of record through."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ModelLedgerRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    row_ts: datetime
    raw_row: str


class ModelLedgerRowsPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelLedgerRow, ...]
    next_cursor: str | None
    record_newest_row_ts: datetime | None


class LedgerReadError(RuntimeError):
    """The read of the ledger of record failed; nothing was answered."""


class ProtocolLedgerRowsReader(Protocol):
    def read_rows(
        self, *, since: datetime, cursor: str | None, page_size: int
    ) -> ModelLedgerRowsPage:
        """One page of rows from ``since``, oldest first; raises LedgerReadError on any failure."""
        ...
