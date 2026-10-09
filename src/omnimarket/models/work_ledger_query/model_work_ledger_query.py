# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and answer of the database work ledger read node (OMN-20738).

Shared so a reader moving off the markdown file (and its shadow comparison)
builds the request and reads the answer without importing the node's package.
Every answer carries its freshness (newest row stamp and projection time) and
the parity status of the UTC days its window covers, so a reader can tell a
difference caused by a row the database lacks from a difference in logic.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_LANE = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
_TICKET = r"^OMN-\d+$"
_PR = r"^[A-Za-z0-9_.-]+#\d+$"


class EnumWorkLedgerQueryKind(StrEnum):
    """What the node answers. Each mirrors one read of the markdown ledger."""

    ROWS = "rows"
    OPEN_CLAIMS = "open-claims"
    HOLDS = "holds"
    INBOX = "inbox"
    NEWEST_PER_LANE = "newest-per-lane"
    RULINGS = "rulings"


class EnumWorkLedgerParityStatus(StrEnum):
    """The window's parity, from the daily parity receipts the ledger holds."""

    EXACT = "exact"
    NOT_EXACT = "not-exact"
    UNMEASURED = "unmeasured"


class ModelWorkLedgerQueryRequest(BaseModel):
    """One question over the database ledger, bounded by a row_ts window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    query: EnumWorkLedgerQueryKind
    since: datetime | None = None
    until: datetime | None = None
    now: datetime | None = None
    lane: str | None = Field(default=None, pattern=_LANE)
    lanes: tuple[Annotated[str, Field(pattern=_LANE)], ...] = ()
    ticket: str | None = Field(default=None, pattern=_TICKET)
    pr: str | None = Field(default=None, pattern=_PR)
    kinds: tuple[str, ...] = ()
    row_ref: str | None = None
    term: str | None = Field(default=None, min_length=1)
    repo: str | None = None
    surface: str | None = None
    to: str | None = Field(default=None, pattern=_LANE)
    claims_since: str | None = None
    limit: int | None = Field(default=None, ge=1)


class ModelWorkLedgerRowRecord(BaseModel):
    """One database row. ``text`` is the row's first line, the line a markdown
    reader matches; ``raw_row`` is the whole row with any continuation lines."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    row_ts: datetime
    row_type: str
    row_lane: str | None = None
    text: str
    raw_row: str
    source: str
    projected_at: datetime | None = None


class ModelWorkLedgerOpenClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    claim: ModelWorkLedgerRowRecord
    ticket: str
    newest_row_stamp: str
    newest_row_kind: str
    rows: int
    host: str
    run: str
    leases: tuple[str, ...]


class ModelWorkLedgerHoldInForce(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hold_id: str
    by: str
    to: str | None = None
    repo: str | None = None
    pr: str | None = None
    surface: str | None = None
    until: str | None = None
    row: ModelWorkLedgerRowRecord


class ModelWorkLedgerHoldCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    checked: int = 0
    released: int = 0
    expired: int = 0
    legacy_no_id: int = 0


class ModelWorkLedgerInboxEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelWorkLedgerRowRecord
    needs_action: bool


class ModelWorkLedgerInbox(BaseModel):
    """MSG rows without an ACK and HOLD rows without a RELEASE, for one lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    entries: tuple[ModelWorkLedgerInboxEntry, ...]


class ModelWorkLedgerFreshness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    newest_row_ts: datetime | None
    newest_projected_at: datetime | None
    rows_read: int


class ModelWorkLedgerParityDay(BaseModel):
    """The newest daily parity receipt for one UTC day, or none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    day: str
    exact: bool | None
    missing: int | None = None
    extra: int | None = None
    receipt_row_id: str | None = None


class ModelWorkLedgerParity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumWorkLedgerParityStatus
    days: tuple[ModelWorkLedgerParityDay, ...]


class ModelWorkLedgerQueryResult(BaseModel):
    """The answer. ``error`` set means the node could not read the database:
    no section is an answer then, and a caller must not read empty as none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    query: EnumWorkLedgerQueryKind
    window_since: datetime | None
    window_until: datetime
    matched: int = 0
    rows: tuple[ModelWorkLedgerRowRecord, ...] = ()
    open_claims: tuple[ModelWorkLedgerOpenClaim, ...] = ()
    holds: tuple[ModelWorkLedgerHoldInForce, ...] = ()
    hold_counts: ModelWorkLedgerHoldCounts | None = None
    inbox: tuple[ModelWorkLedgerInbox, ...] = ()
    freshness: ModelWorkLedgerFreshness | None = None
    parity: ModelWorkLedgerParity | None = None
    error: str | None = None


__all__ = [
    "EnumWorkLedgerParityStatus",
    "EnumWorkLedgerQueryKind",
    "ModelWorkLedgerFreshness",
    "ModelWorkLedgerHoldCounts",
    "ModelWorkLedgerHoldInForce",
    "ModelWorkLedgerInbox",
    "ModelWorkLedgerInboxEntry",
    "ModelWorkLedgerOpenClaim",
    "ModelWorkLedgerParity",
    "ModelWorkLedgerParityDay",
    "ModelWorkLedgerQueryRequest",
    "ModelWorkLedgerQueryResult",
    "ModelWorkLedgerRowRecord",
]
