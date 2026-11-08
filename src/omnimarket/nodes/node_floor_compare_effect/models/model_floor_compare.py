# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the floor's running comparison (OMN-23018).

A history is a list of dated snapshots; each snapshot holds only the numbers that were read at that moment. A key a
snapshot does not hold was not read then, which the reader reports as ``unread`` and never as zero.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    source: str = Field(
        default="",
        description="Where the number is read from, now and in the backfilled history (empty: the published floor only).",
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

    kind: Literal["floor_simple", "factory_record", "factory_measures"]
    glob: str = Field(min_length=1, description="Relative to the registry home.")


class ModelLedgerPattern(_Frozen):
    key: str = Field(min_length=1)
    row_types: tuple[str, ...] = Field(min_length=1)
    pattern: str = Field(min_length=1)


class ModelHistorySources(_Frozen):
    """The dated sources the history is backfilled from and the live tick reads again for today's value (OMN-23018 backfill).

    Every count is taken over the trailing ``window_hours`` ending at the point it is stored at, so a backfilled point and
    today's value are the same measure. A source with no record in that window gives no value at all: unread, never zero.
    """

    window_hours: int = Field(ge=1)
    backfill_days: int = Field(ge=1)
    grid_hours: int = Field(
        ge=1, description="Distance between stored points of the counted sources."
    )
    friction_grid_hours: int = Field(ge=1)
    friction_grid_minute: int = Field(ge=0, le=59)
    min_alive: dict[str, int] = Field(
        description="Per counted source, the records of any kind it must hold in the window, or its counts are unread.",
    )
    level_max_age_minutes: int = Field(
        ge=1,
        description="The latest logged level counts as today's only when it is this recent.",
    )
    min_tick_samples: int = Field(
        ge=1,
        description="The throughput tick log needs this many samples in the window for a per-tick mean to count.",
    )
    ledger_env: str
    ledger_archive_dirname: str
    ledger_lane_outcomes: tuple[str, ...]
    friction_umbrella_ticket: str
    friction_rules_node: str
    friction_rules_file: str
    friction_latest_relpath: str
    refusal_class_prefix: str
    throughput_relpath: str
    lab_alarms_relpath: str
    lab_alarm_runs_relpath: str
    floor_alarm_runs_relpath: str
    landing_env: str
    landing_decisions_file: str
    landing_refusal_outcomes: tuple[str, ...]
    git_clone_glob: str = Field(
        description="Canonical clones are found under the registry home by this glob; the key is the directory name."
    )
    git_timeout_seconds: int = Field(ge=1)
    ledger_patterns: tuple[ModelLedgerPattern, ...] = Field(
        default=(),
        description="Keys counted from ledger rows whose type is listed and whose text matches the pattern (OMN-23081).",
    )


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
    estimated_cost_stoppages: tuple[str, ...] = Field(
        default=(),
        description="Stoppage ids whose logged cost is an estimate of idle capacity, not lost work anything measured; the floor leaves it out, so the history does too.",
    )
    backfill: tuple[ModelBackfillSource, ...] = ()
    repository_names_overlay_relpath: str = Field(
        description="A host's own repository names overlay under the registry home; read when it exists and no env var names one (OMN-23134).",
    )
    history: ModelHistorySources
    emission_journal_relpath: str
    emission_node: str
    emission_piece: int = Field(ge=1, le=18)


class ModelSnapshot(_Frozen):
    """The numbers read at one moment. ``source`` says where the moment came from (a tick or a backfilled copy)."""

    at: datetime
    source: str
    values: dict[str, float]
    labels: dict[str, str] = Field(default_factory=dict)
    complete: tuple[str, ...] = Field(
        default=(),
        description=(
            "Key prefixes this snapshot lists in full: a key under one of them that the snapshot does not hold was "
            "read as absent from the source, which is zero. Under any other prefix a missing key is unread."
        ),
    )

    @field_validator("at")
    @classmethod
    def _at_has_a_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("a snapshot's time must carry a timezone")
        return value


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
    # Each live rebuild piece against the old way (OMN-23081); typed by model_piece_measure.
    pieces: tuple[dict[str, Any], ...] = ()


class ModelFloorCompareRequest(_Frozen):
    home: str | None = None
    now: datetime | None = None
    drill_error: bool = False
    backfill_only: bool = False
    backfill_history: bool = False
    history_days: int | None = None

    @field_validator("now")
    @classmethod
    def _now_has_a_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("now must carry a timezone")
        return value


class ModelFloorCompareResult(_Frozen):
    ok: bool
    wrote: str | None = None
    recorded: bool = False
    backfilled: int = 0
    history_points: int = 0
    failure_code: str | None = None
    failure_detail: str = ""
    history_written: dict[str, int] = Field(default_factory=dict)
    source_notes: tuple[str, ...] = ()
