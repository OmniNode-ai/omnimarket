# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One rows read the store bounded before it sent the rows (OMN-23606)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.work_ledger_query.model_work_ledger_query import (
    ModelWorkLedgerRowRecord,
)


class ModelWorkLedgerRowsBounded(BaseModel):
    """The rows a bounded read kept, oldest first, with what the handler needs from the rows it did not load.

    ``matched`` is how many rows the store's own filters kept in the window (set only when the read was cut to the
    newest ``newest`` rows, otherwise the handler counts what it received). ``first_row_ts`` is the oldest row of the
    window before any filter (set only when the request named no start), the date the parity days begin on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: tuple[ModelWorkLedgerRowRecord, ...] = ()
    matched: int | None = Field(default=None, ge=0)
    first_row_ts: datetime | None = None
