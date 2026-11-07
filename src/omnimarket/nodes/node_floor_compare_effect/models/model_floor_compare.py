# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the floor's running comparison (OMN-23018).

A history is a list of dated snapshots; each snapshot holds only the numbers that were read at that moment. A key a
snapshot does not hold was not read then, which the reader reports as ``unread`` and never as zero.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


Polarity = Literal["higher_is_better", "lower_is_better"]
Verdict = Literal["better", "worse", "same", "unread"]


class ModelMetricFamily(_Frozen):
    """How one family of keys (by key prefix) reads: unit, the direction that counts as better, and the plain words."""

    prefix: str = Field(min_length=1)
    polarity: Polarity
    unit: str
    noun: str = Field(
        min_length=1,
        description="What the line calls the thing, for ``{noun}`` of the sentence.",
    )


class ModelComparisonWindow(_Frozen):
    days: int = Field(ge=1)
    label: str = Field(min_length=1)
    tolerance_hours: float = Field(
        gt=0,
        description="A snapshot counts as the reading at now minus days only inside this distance.",
    )


class ModelBackfillSource(_Frozen):
    """A directory of dated copies read once into the history: ``kind`` names the parser."""

    kind: Literal["floor_simple", "factory_record"]
    glob: str = Field(min_length=1, description="Relative to the registry home.")


class ModelFloorCompareRuntime(_Frozen):
    home_env: str
    simple_relpath: str
    record_relpath: str
    history_relpath: str
    output_relpath: str
    retention_days: int = Field(ge=1)
    min_gap_seconds: int = Field(
        ge=0,
        description="A tick closer than this to the previous snapshot of the same source is not stored again.",
    )
    max_lines_per_window: int = Field(ge=1)
    windows: tuple[ModelComparisonWindow, ...] = Field(min_length=1)
    families: tuple[ModelMetricFamily, ...] = Field(min_length=1)
    piece_rank: dict[str, int]
    stoppage_cost_pattern: str
    backfill: tuple[ModelBackfillSource, ...] = ()
    emission_journal_relpath: str
    emission_node: str
    emission_piece: int = Field(ge=1, le=18)


class ModelSnapshot(_Frozen):
    """The numbers read at one moment. ``source`` says where the moment came from (a tick or a backfilled copy)."""

    at: datetime
    source: str
    values: dict[str, float]
    labels: dict[str, str] = Field(default_factory=dict)


class ModelReading(_Frozen):
    """The value of one key at one moment, or unread with the reason."""

    key: str
    read: bool
    value: float | None = None
    at: datetime | None = None
    reason: str = ""


class ModelCompareLine(_Frozen):
    key: str
    verdict: Verdict
    old: float | None
    new: float | None
    old_text: str
    new_text: str
    better_is: Polarity
    direction_text: str
    words: str
    old_read_at: datetime | None = None


class ModelWindowComparison(_Frozen):
    days: int
    label: str
    target: datetime
    better: tuple[ModelCompareLine, ...]
    worse: tuple[ModelCompareLine, ...]
    same: int
    unread: tuple[ModelCompareLine, ...]
    counts: dict[str, int]


class ModelFloorComparison(_Frozen):
    schema_id: Literal["omni.floor-compare/1"] = "omni.floor-compare/1"
    generated_at: datetime
    history_points: int
    oldest_point: datetime | None
    windows: tuple[ModelWindowComparison, ...]


class ModelFloorCompareRequest(_Frozen):
    home: str | None = None
    now: datetime | None = None
    drill_error: bool = False
    backfill_only: bool = False


class ModelFloorCompareResult(_Frozen):
    ok: bool
    wrote: str | None = None
    recorded: bool = False
    backfilled: int = 0
    history_points: int = 0
    failure_code: str | None = None
    failure_detail: str = ""
