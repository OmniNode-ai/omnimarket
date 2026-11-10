# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed boundaries of the accepted-row topic and the rebuild from it (OMN-22915, L12).

The accepted-row topic carries every row the judge accepted, once, with its sequence. History up
to the watermark is published once marked ``bootstrap``, ordered by ``legacy_ordinal``; live rows
follow ordered by ``ledger_seq``. A table rebuilt from the topic must equal the live table row for
row and sequence for sequence.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_work_ledger_entity_kind import EnumWorkLedgerEntityKind
from omnimarket.models.model_work_ledger_fold_result import ModelWorkLedgerRowRecord


class EnumAcceptedRowSource(StrEnum):
    BOOTSTRAP = "bootstrap"
    LIVE = "live"


class ModelAcceptedRowEvent(BaseModel):
    """One accepted row as the accepted-row topic carries it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    ledger_id: str = Field(min_length=1)
    row_id: str = Field(pattern=r"^[0-9a-f]{64}$", description="sha256 of raw_row")
    raw_row: str = Field(min_length=1)
    source: EnumAcceptedRowSource
    ledger_seq: int | None = Field(default=None, ge=1)
    legacy_ordinal: int | None = Field(default=None, ge=1)
    req: str | None = Field(
        default=None, description="The command's req= cell, when it had one."
    )


class ModelRebuiltEntity(BaseModel):
    """One ``work_ledger_state`` entity: the same columns the writer's guarded upserts keep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_key: str
    kind: EnumWorkLedgerEntityKind
    lane: str | None = None
    ticket: str | None = None
    repo: str | None = None
    pr: str | None = None
    scope_to: str | None = None
    scope_surface: str | None = None
    until_at: datetime | None = None
    detail: str | None = None
    opened_at: datetime | None = None
    opened_row_id: str | None = None
    closed_at: datetime | None = None
    closed_row_id: str | None = None
    is_open: bool


class ModelWorkLedgerTable(BaseModel):
    """A work-ledger table: the log rows and the entity state, in a canonical order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelWorkLedgerRowRecord, ...] = ()
    state: tuple[ModelRebuiltEntity, ...] = ()


class ModelRebuildRequest(BaseModel):
    """Accepted-row events in topic order, and optionally the live table to compare with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: tuple[ModelAcceptedRowEvent, ...]
    live: ModelWorkLedgerTable | None = None


class ModelTableDifference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    where: str = Field(description="rows or state")
    difference: str


class ModelRebuildResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rebuilt: ModelWorkLedgerTable
    compared: bool
    equal: bool
    differences: tuple[ModelTableDifference, ...] = ()


__all__: list[str] = [
    "EnumAcceptedRowSource",
    "ModelAcceptedRowEvent",
    "ModelRebuildRequest",
    "ModelRebuildResult",
    "ModelRebuiltEntity",
    "ModelTableDifference",
    "ModelWorkLedgerTable",
]
