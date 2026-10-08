# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read boundary the ledger_seq gap check reads the database through (OMN-20541)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.models.model_work_ledger_seq_gap_report import WorkLedgerSeqFacts


class ProtocolWorkLedgerSeqReader(Protocol):
    def read_gap_facts(
        self,
        *,
        ledger_id: str,
        from_seq: int,
        since: datetime | None,
        until: datetime | None,
    ) -> WorkLedgerSeqFacts: ...


__all__: list[str] = ["ProtocolWorkLedgerSeqReader"]
