# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result models of the markdown work-ledger projection (OMN-20275).

A record is ``(ledger_id, ledger_seq)``. ``row_id`` (the sha256 of the raw row) is a
content check and never the identity: two distinct rows can carry the same text.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_work_ledger_render_row import ModelWorkLedgerRenderRow


class EnumWorkLedgerRenderKind(StrEnum):
    """ROW renders one row event; PROBE reads the ledger of record's head (the lag probe)."""

    ROW = "row"
    PROBE = "probe"


class EnumWorkLedgerBlockReason(StrEnum):
    SOURCE_MISSING = "source_missing"
    SOURCE_UNAVAILABLE = "source_unavailable"
    ROW_ID_CONFLICT = "row_id_conflict"
    MULTI_LINE_ROW = "multi_line_row"


class ModelWorkLedgerRenderRequest(BaseModel):
    """One step of the node: a row event with its bus coordinate, or a head probe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumWorkLedgerRenderKind
    row: ModelWorkLedgerRenderRow | None = None
    topic: str | None = None
    offset: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _row_events_carry_their_row(self) -> ModelWorkLedgerRenderRequest:
        if self.kind is EnumWorkLedgerRenderKind.ROW and self.row is None:
            raise ValueError("a ROW request carries its row")
        if self.kind is EnumWorkLedgerRenderKind.PROBE and self.row is not None:
            raise ValueError("a PROBE request carries no row")
        return self


class ModelWorkLedgerRenderBlock(BaseModel):
    """Why the node renders nothing past ``seq``; reported, never silent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    reason: EnumWorkLedgerBlockReason
    detail: str = ""


class ModelWorkLedgerRenderResult(BaseModel):
    """What one step did. ``commit_offsets`` are the per-topic offsets newly safe to
    commit: the longest read prefix whose seqs are all rendered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumWorkLedgerRenderKind
    ledger_id: str
    baseline_seq: int
    watermark: int
    rendered_seqs: tuple[int, ...] = ()
    repaired_seqs: tuple[int, ...] = ()
    dropped_duplicate: bool = False
    committable: bool = True
    commit_offsets: dict[str, int] = Field(default_factory=dict)
    pending_count: int = 0
    source_head: int | None = None
    head_unavailable: bool = False
    blocked: ModelWorkLedgerRenderBlock | None = None


__all__: list[str] = [
    "EnumWorkLedgerBlockReason",
    "EnumWorkLedgerRenderKind",
    "ModelWorkLedgerRenderBlock",
    "ModelWorkLedgerRenderRequest",
    "ModelWorkLedgerRenderResult",
    "ModelWorkLedgerRenderRow",
]
