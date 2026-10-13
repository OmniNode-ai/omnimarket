# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger_seq gap report and the database facts it is built from (OMN-20541)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_RANGES = 50


class ModelSeqRange(BaseModel):
    """An inclusive missing sequence range."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    first: int = Field(ge=1)
    last: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> ModelSeqRange:
        if self.last < self.first:
            raise ValueError("last must be at least first")
        return self


class ModelWorkLedgerSeqGapReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_id: str
    window_since: datetime | None
    window_until: datetime | None
    from_seq: int
    to_seq: int | None
    rows_with_seq: int
    rows_without_seq: int
    first_missing_seq: int | None
    missing_count: int
    missing_ranges: tuple[ModelSeqRange, ...] = Field(max_length=MAX_RANGES)
    missing_ranges_truncated: bool
    duplicate_seqs: tuple[int, ...] = Field(max_length=MAX_RANGES)
    duplicate_count: int
    contiguous_through: int | None
    max_seq: int | None
    max_seq_row_ts: datetime | None
    newest_row_ts: datetime | None
    expected_max_seq: int | None
    tail_missing: int
    exact: bool


@dataclass(frozen=True)
class WorkLedgerSeqFacts:
    """The inputs to build_report, from one database snapshot."""

    ledger_id: str
    from_seq: int
    gaps: tuple[tuple[int, int], ...]
    duplicates: tuple[int, ...]
    duplicate_count: int
    rows_with_seq: int
    rows_without_seq: int
    min_seq: int | None
    max_seq: int | None
    max_seq_row_ts: datetime | None
    newest_row_ts: datetime | None
    window_since: datetime | None = None
    window_until: datetime | None = None
    expected_max_seq: int | None = None


__all__: list[str] = [
    "MAX_RANGES",
    "ModelSeqRange",
    "ModelWorkLedgerSeqGapReport",
    "WorkLedgerSeqFacts",
]
