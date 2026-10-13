# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Fold result: the log record and the state operations one row produces (OMN-19513)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_work_ledger.models.enum_work_ledger_entity_kind import (
    EnumWorkLedgerEntityKind,
    EnumWorkLedgerStateOp,
)


class ModelWorkLedgerRowRecord(BaseModel):
    """One row of ``work_ledger_rows``: the whole log, for parity and rebuild."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    ledger_id: str
    row_ts: datetime
    row_type: str
    row_lane: str | None
    tickets: tuple[str, ...]
    raw_row: str
    source: str
    ledger_seq: int | None = None


class ModelWorkLedgerStateOp(BaseModel):
    """One write to ``work_ledger_state``: the opening facts or the closing fact.

    ``OPEN`` writes the descriptor columns and ``opened_at``; ``CLOSE`` writes
    only ``closed_at``. Each is guarded on ``(at, row_id)`` so a redelivered or
    reordered row cannot move an entity backwards.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_key: str
    kind: EnumWorkLedgerEntityKind
    op: EnumWorkLedgerStateOp
    at: datetime
    row_id: str
    lane: str | None = None
    ticket: str | None = None
    repo: str | None = None
    pr: str | None = None
    scope_to: str | None = None
    scope_surface: str | None = None
    until_at: datetime | None = None
    detail: str | None = None


class ModelWorkLedgerFoldResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelWorkLedgerRowRecord
    ops: tuple[ModelWorkLedgerStateOp, ...] = ()


__all__: list[str] = [
    "ModelWorkLedgerFoldResult",
    "ModelWorkLedgerRowRecord",
    "ModelWorkLedgerStateOp",
]
