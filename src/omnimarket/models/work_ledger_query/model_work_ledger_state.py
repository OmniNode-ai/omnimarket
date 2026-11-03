# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The state-backed read of the work ledger point queries (OMN-21948).

``work_ledger_state`` keeps one row per entity (an open claim, a live hold, an
unanswered message, a ruling) with a generated ``is_open`` column. A reader
answers a :class:`ModelWorkLedgerStateQuery` with one keyset-ordered page of
the open set, joined to the row that opened each entity so the verb's own line
can be rendered from it, plus the table's watermark.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.work_ledger_query.model_work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerRowRecord,
)

_LANE = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"


class ModelWorkLedgerStateCursor(BaseModel):
    """The last entity of a page: the next page starts strictly after it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    opened_at: AwareDatetime
    entity_key: str = Field(min_length=1)


class ModelWorkLedgerStateQuery(BaseModel):
    """One page request to a state reader. Filters mirror the verb's arguments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    point: EnumWorkLedgerQueryKind
    now: AwareDatetime
    limit: int = Field(ge=1)
    after: ModelWorkLedgerStateCursor | None = None
    lane: str | None = None
    lanes: tuple[Annotated[str, Field(pattern=_LANE)], ...] = ()
    ticket: str | None = None
    repo: str | None = None
    pr: str | None = None
    surface: str | None = None
    to: str | None = None
    claims_since: datetime | None = None


class ModelWorkLedgerLaneActivity(BaseModel):
    """What the ledger holds for a claim's lane since the claim opened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: int = Field(ge=0)
    newest_row_ts: datetime
    newest_row_kind: str
    leases: tuple[str, ...] = ()


class ModelWorkLedgerStateEntity(BaseModel):
    """One open ``work_ledger_state`` entity and the row that opened it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_key: str
    kind: str
    lane: str | None = None
    ticket: str | None = None
    repo: str | None = None
    pr: str | None = None
    scope_to: str | None = None
    scope_surface: str | None = None
    until_at: datetime | None = None
    opened_at: AwareDatetime
    opened_row: ModelWorkLedgerRowRecord
    activity: ModelWorkLedgerLaneActivity | None = None


class ModelWorkLedgerStatePage(BaseModel):
    """A page of the open set. ``total``, ``released`` and ``expired`` count the
    state table's entities of the queried kind; ``watermark`` is the newest
    ``projected_at`` of the table at the read and ``ledger_newest`` the newest
    row stamp of the log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entities: tuple[ModelWorkLedgerStateEntity, ...] = ()
    watermark: AwareDatetime | None = None
    has_more: bool = False
    total: int = Field(default=0, ge=0)
    released: int = Field(default=0, ge=0)
    expired: int = Field(default=0, ge=0)
    ledger_newest: datetime | None = None


__all__ = [
    "ModelWorkLedgerLaneActivity",
    "ModelWorkLedgerStateCursor",
    "ModelWorkLedgerStateEntity",
    "ModelWorkLedgerStatePage",
    "ModelWorkLedgerStateQuery",
]
