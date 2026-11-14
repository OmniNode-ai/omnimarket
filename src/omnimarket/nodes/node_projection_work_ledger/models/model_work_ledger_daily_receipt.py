# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the exact UTC daily receipt: the ledger table against the accepted-row topic (OMN-23153).

The receipt compares two lists of one UTC day's rows and nothing else: the rows in the ledger table
(``work_ledger_rows``) and the rows the accepted-row topic carries. A row in the topic and not in the
table is ``missing``; a row in the table and not in the topic is ``extra`` (a row planted in the
projection alone is the defect the receipt exists to catch); a row on both sides under different
sequence or time is ``mismatched``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumDailyReceiptState(StrEnum):
    NOT_CLOSED = "NOT-CLOSED"
    GREEN = "GREEN"
    RED = "RED"


class ModelDailyReceiptConfig(BaseModel):
    """The receipt's fixed time, read from the node's contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cutoff_utc: str = Field(
        ...,
        pattern=r"^\d{2}:\d{2}$",
        description="UTC wall time of the day's cutoff, on the day after the receipt's day.",
    )
    close_minutes: int = Field(
        ..., ge=1, description="Minutes after the cutoff at which the window closes."
    )
    lane: str = Field(
        ..., min_length=1, description="The lane cell of the receipt row."
    )


class ModelDailyReceiptRow(BaseModel):
    """One row as either source lists it: its content hash, sequence and time, and when it was first seen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    ledger_seq: int | None = Field(default=None, ge=1)
    row_ts: datetime
    seen_at: datetime | None = Field(
        default=None,
        description="When the source first held the row; None means before the window closed.",
    )


class ModelDailyReceiptRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    day: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    now: datetime
    table_rows: tuple[ModelDailyReceiptRow, ...]
    topic_rows: tuple[ModelDailyReceiptRow, ...]
    verb_diff: dict[str, int | None] | None = Field(
        default=None,
        description="Per read verb, the answer lines file and database do not share (OMN-21885); None = verbs not run.",
    )


class ModelDailyReceipt(BaseModel):
    """The receipt for one UTC day. ``exact`` needs rows on both sides and zero of every difference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    day: str
    state: EnumDailyReceiptState
    cutoff_at: datetime
    closes_at: datetime
    table_row_ids: tuple[str, ...] = ()
    topic_row_ids: tuple[str, ...] = ()
    missing_row_ids: tuple[str, ...] = ()
    extra_row_ids: tuple[str, ...] = ()
    mismatched_row_ids: tuple[str, ...] = ()
    conflicting_row_ids: tuple[str, ...] = ()
    late_rows: int = Field(
        default=0,
        ge=0,
        description="Rows first seen after the window closed; left out of both sides.",
    )
    missing: int = Field(default=0, ge=0)
    extra: int = Field(default=0, ge=0)
    mismatched: int = Field(default=0, ge=0)
    verb_diff: dict[str, int | None] | None = None
    exact: bool = False


__all__: list[str] = [
    "EnumDailyReceiptState",
    "ModelDailyReceipt",
    "ModelDailyReceiptConfig",
    "ModelDailyReceiptRequest",
    "ModelDailyReceiptRow",
]
