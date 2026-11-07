# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the reading: a verdict computed from ledger rows in a closed window.

Shared by node_reading_compute (the pure function) and node_reading_writer_effect (the only
writer of reading rows), so neither imports the other's package.

A reading is WORKING, NOT_WORKING or UNKNOWN. The row word for NOT_WORKING has an underscore
so that a row cell stays one token; the displayed words are WORKING, NOT WORKING and UNKNOWN.
UNKNOWN and stale count as NOT WORKING for anything that gates on a reading (``blocks``).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumReadingVerdict(StrEnum):
    WORKING = "WORKING"
    NOT_WORKING = "NOT_WORKING"
    UNKNOWN = "UNKNOWN"

    @property
    def display(self) -> str:
        return self.value.replace("_", " ")

    @property
    def blocks(self) -> bool:
        """Anything but WORKING blocks the pieces above this one."""
        return self is not EnumReadingVerdict.WORKING


class EnumReadingOutcome(StrEnum):
    """How a reading row came to be.

    ``read`` and ``window-failed`` are computed readings and are immutable under their row key.
    ``source-unreadable`` and ``replay-failed`` are records of an attempt and are not.
    """

    READ = "read"
    WINDOW_FAILED = "window-failed"
    SOURCE_UNREADABLE = "source-unreadable"
    REPLAY_FAILED = "replay-failed"

    @property
    def is_computed(self) -> bool:
        return self in (EnumReadingOutcome.READ, EnumReadingOutcome.WINDOW_FAILED)


class ModelLedgerEvidenceRow(BaseModel):
    """One ledger row as evidence: its stamp, type, cells and exact text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stamp: datetime
    row_type: str
    lane: str | None = None
    cells: dict[str, str] = Field(default_factory=dict)
    raw: str
    ingested_at: datetime | None = None

    @property
    def key(self) -> str:
        """The row's identity: its ``req`` cell, else its stamp, type and lane."""
        request = self.cells.get("req")
        if request:
            return f"req:{request}"
        return f"row:{self.stamp.isoformat()}|{self.row_type}|{self.lane or ''}"


class EnumCellOp(StrEnum):
    EQ = "eq"
    NE = "ne"
    IN = "in"


class ModelCellPredicate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cell: str = Field(min_length=1)
    op: EnumCellOp
    value: str | None = None
    values: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> ModelCellPredicate:
        if self.op is EnumCellOp.IN:
            if not self.values or self.value is not None:
                raise ValueError("op in takes values and no value")
        elif self.value is None or self.values:
            raise ValueError(f"op {self.op.value} takes one value")
        return self

    def holds(self, cells: dict[str, str]) -> bool:
        found = cells.get(self.cell)
        if self.op is EnumCellOp.EQ:
            return found == self.value
        if self.op is EnumCellOp.NE:
            return found is not None and found != self.value
        return found in self.values


class EnumSourceMode(StrEnum):
    #: The newest matching row decides (a state: the latest parity receipt).
    NEWEST = "newest"
    #: At least ``min_rows`` matching rows must pass within the window (a liveness).
    COUNT = "count"


class ModelReadingSource(BaseModel):
    """One ledger source of a piece's reading: which rows, and when they pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1)
    row_type: str = Field(min_length=1)
    lane: str | None = None
    term: str | None = None
    cells: dict[str, str] = Field(default_factory=dict)
    mode: EnumSourceMode
    pass_when: tuple[ModelCellPredicate, ...] = ()
    min_rows: int = Field(default=1, ge=1)
    max_age_seconds: int = Field(ge=1)

    def selects(self, row: ModelLedgerEvidenceRow) -> bool:
        if row.row_type != self.row_type:
            return False
        if self.lane is not None and row.lane != self.lane:
            return False
        if self.term is not None and self.term not in row.raw:
            return False
        return all(row.cells.get(cell) == value for cell, value in self.cells.items())

    def passes(self, row: ModelLedgerEvidenceRow) -> bool:
        return all(predicate.holds(row.cells) for predicate in self.pass_when)


class ModelLandingRequirements(BaseModel):
    """The landing goal's hourly requirements, read for piece 9 (plan section 3.5, row 9).

    An eligible head is open, in scope, non-draft and green on every required context. The reading fails
    when an eligible head has waited over ``head_max_age_seconds``; or, while at least ``waiting_threshold``
    heads wait, when fewer than ``goal_merges_per_window`` merged inside ``merge_window_seconds``; or, while
    fewer wait, when the daily synthetic landing canary has not landed (``canary`` names its row). A watcher
    snapshot older than ``snapshot_max_age_seconds`` is stale and reads NOT WORKING.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    head_max_age_seconds: int = Field(ge=1)
    goal_merges_per_window: int = Field(ge=1)
    merge_window_seconds: int = Field(ge=60)
    waiting_threshold: int = Field(ge=1)
    snapshot_max_age_seconds: int = Field(ge=1)
    canary: ModelReadingSource

    @model_validator(mode="after")
    def _shape(self) -> ModelLandingRequirements:
        if self.merge_window_seconds % 60:
            raise ValueError("merge_window_seconds is a whole number of minutes")
        return self


class ModelLandingHead(BaseModel):
    """One eligible head and how long its checks have been green without a merge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str = Field(min_length=1)
    waiting_seconds: int = Field(ge=0)
    #: Green but the branch conflicts, so it cannot be armed; still waiting.
    conflicted: bool = False


class ModelLandingSnapshot(BaseModel):
    """What the pull request watcher says about landing, read once by the writer's landing source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: How old the watcher's last tick is at the read.
    age_seconds: int = Field(ge=0)
    heads: tuple[ModelLandingHead, ...] = ()
    merges_in_window: int = Field(ge=0)


class ModelPieceReadingSpec(BaseModel):
    """What one piece's reading reads today, and the fault that must flip it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    piece: int = Field(ge=1)
    name: str = Field(min_length=1)
    readable_today: bool
    #: Why a piece reads UNKNOWN: the table's lab trigger part has no producer yet.
    unknown_reason: str | None = None
    #: A token for the row's ``basis`` cell when the producer is narrower than the table's.
    basis: str | None = None
    sources: tuple[ModelReadingSource, ...] = ()
    #: Piece 9 reads from the watcher's snapshot against the landing goal's requirements, not from ledger rows alone.
    landing: ModelLandingRequirements | None = None
    #: The fault injected on purpose; the reading must then go NOT WORKING.
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelPieceReadingSpec:
        if self.readable_today:
            if not self.sources and self.landing is None:
                raise ValueError(f"piece {self.piece}: readable today needs a source")
        else:
            if self.sources or self.landing is not None:
                raise ValueError(
                    f"piece {self.piece}: a piece that is not readable today has no source or landing requirements"
                )
            if not self.unknown_reason:
                raise ValueError(
                    f"piece {self.piece}: UNKNOWN needs an unknown_reason (never blank)"
                )
        ids = [source.source_id for source in self.all_sources]
        if len(ids) != len(set(ids)):
            raise ValueError(f"piece {self.piece}: source ids repeat")
        return self

    @property
    def all_sources(self) -> tuple[ModelReadingSource, ...]:
        """The ledger sources of the reading: its own, and the landing canary's."""
        if self.landing is None:
            return self.sources
        return (*self.sources, self.landing.canary)

    @property
    def lookback_seconds(self) -> int:
        return max((source.max_age_seconds for source in self.all_sources), default=0)


class ModelReadingSourceRows(BaseModel):
    """The rows one source read, or why it could not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    rows: tuple[ModelLedgerEvidenceRow, ...] = ()
    error: str | None = None


class ModelReadingComputeRequest(BaseModel):
    """Everything a reading is a function of; it reads no clock, file or network."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec: ModelPieceReadingSpec
    window_start: datetime
    window_end: datetime
    cutoff: datetime
    order_hash: str = Field(min_length=1)
    overlay_hash: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    sources: tuple[ModelReadingSourceRows, ...] = ()
    #: The watcher snapshot of a landing piece, or why it could not be read.
    landing: ModelLandingSnapshot | None = None
    landing_error: str | None = None


class ModelReadingComputeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    piece: int
    name: str
    verdict: EnumReadingVerdict
    outcome: EnumReadingOutcome
    #: Token-safe reason codes, no spaces or pipes, in a fixed order.
    reasons: tuple[str, ...]
    basis: str | None = None
    window_start: datetime
    window_end: datetime
    order_hash: str
    overlay_hash: str
    revision: str
    manifest_hash: str
    rows_read: int
    #: piece, window start, order hash, overlay hash and revision.
    row_key: str
    #: Hash of what the reading says, not of when it was run.
    content_hash: str
    injected_fault: str


class ModelReadingWindowCommand(BaseModel):
    """Run the readings of one closed window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    window_start: datetime
    #: Only these pieces; empty means every piece the overlay declares.
    pieces: tuple[int, ...] = ()
    #: A probe reads and computes and writes no row.
    write: bool = True


class ModelReadingPieceOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    piece: int
    verdict: EnumReadingVerdict
    outcome: EnumReadingOutcome
    reasons: tuple[str, ...]
    row_key: str
    #: ``written``, ``already-written``, ``not-written`` (a probe) or ``append-failed:<why>``.
    ledger: str
    row: str | None = None


class ModelReadingWindowResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    window_start: datetime | None = None
    window_end: datetime | None = None
    cutoff: datetime | None = None
    failure: str | None = None
    pieces: tuple[ModelReadingPieceOutcome, ...] = ()

    @property
    def failed_pieces(self) -> tuple[ModelReadingPieceOutcome, ...]:
        return tuple(
            piece
            for piece in self.pieces
            if piece.outcome is not EnumReadingOutcome.READ
            or piece.ledger.startswith("append-failed")
        )
