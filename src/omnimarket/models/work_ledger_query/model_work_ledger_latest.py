# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The newest-row-per-subject state of the work ledger (OMN-23606).

``work_ledger_latest`` keeps, per subject (a lane or a ticket) and per tracked
row type (CLAIM, TERMINAL, STATUS, MSG, HOLD), the newest row and the few cells
a reader decides on. It is state, not history: a board or a lane placement that
needs "when was this ticket last claimed, closed, asked about" reads one small
page of this table instead of paging every row of a window.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class EnumWorkLedgerLatestSubject(StrEnum):
    """What a latest entry is about."""

    LANE = "lane"
    TICKET = "ticket"


class ModelWorkLedgerLatestEntry(BaseModel):
    """The newest row of one row type for one subject, with the cells a reader decides on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_kind: EnumWorkLedgerLatestSubject
    subject: str = Field(min_length=1)
    row_type: str = Field(min_length=1)
    row_id: str = Field(min_length=1)
    at: AwareDatetime
    lane: str | None = None
    ticket: str | None = None
    pr: str | None = None
    outcome: str | None = None
    to: str | None = None


class ModelWorkLedgerLatestCursor(BaseModel):
    """The last entry of a page: the next page starts strictly after it, in key order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_kind: EnumWorkLedgerLatestSubject
    subject: str = Field(min_length=1)
    row_type: str = Field(min_length=1)


class ModelWorkLedgerLatestQuery(BaseModel):
    """One page request to a latest-state reader."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    limit: int = Field(ge=1)
    after: ModelWorkLedgerLatestCursor | None = None
    subject_kind: EnumWorkLedgerLatestSubject | None = None
    subjects: tuple[str, ...] = ()
    row_types: tuple[str, ...] = ()
    since: AwareDatetime | None = None


class ModelWorkLedgerLatestPage(BaseModel):
    """One keyset page in key order, the table's watermark and whether more follow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ModelWorkLedgerLatestEntry, ...] = ()
    watermark: AwareDatetime | None = None
    has_more: bool = False


class ModelWorkLedgerLatestOp(BaseModel):
    """One write to ``work_ledger_latest``; guarded on ``(at, row_id)`` so a redelivered or reordered row cannot move an entry backwards."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry: ModelWorkLedgerLatestEntry


__all__: list[str] = [
    "EnumWorkLedgerLatestSubject",
    "ModelWorkLedgerLatestCursor",
    "ModelWorkLedgerLatestEntry",
    "ModelWorkLedgerLatestOp",
    "ModelWorkLedgerLatestPage",
    "ModelWorkLedgerLatestQuery",
]
