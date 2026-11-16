# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One ledger row as the work_ledger_rows projection stores it."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict


class ModelLedgerRow(BaseModel):
    """The five columns the matcher reads; cells are parsed from ``raw_row``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    row_ts: AwareDatetime
    row_type: str
    row_lane: str | None
    raw_row: str
