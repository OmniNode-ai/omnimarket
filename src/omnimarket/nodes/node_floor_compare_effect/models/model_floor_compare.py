# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the floor's running comparison (OMN-23018).

A history is a list of dated snapshots; each snapshot holds only the numbers that were read at that moment. A key a
snapshot does not hold was not read then, which the reader reports as ``unread`` and never as zero.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class ModelCompareCategory(_Frozen):
    """One group of lines on the page: a plain-words title and the key prefixes that belong to it (longest prefix wins)."""

    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    title: str = Field(min_length=1)
    prefixes: tuple[str, ...] = Field(default=())

    @field_validator("prefixes")
    @classmethod
    def _prefixes_are_not_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not p for p in value):
            raise ValueError("a category prefix must not be empty")
        return value


class ModelFloorCompareOverlay(_Frozen):
    """The category grouping of the compare page, supplied by the deployment overlay and never packaged.

    ``categories`` is the fixed set the cards show, in order. A line no prefix claims goes to ``other`` so no line is ever
    dropped from a roll-up; ``other``'s card is shown only when it holds a line.
    """

    categories: tuple[ModelCompareCategory, ...] = Field(min_length=1)
    other: ModelCompareCategory

    @model_validator(mode="after")
    def _grouping_is_unambiguous(self) -> ModelFloorCompareOverlay:
        ids = [c.id for c in (*self.categories, self.other)]
        if len(set(ids)) != len(ids):
            raise ValueError("category ids must be unique")
        if self.other.prefixes:
            raise ValueError("the other category claims no prefix")
        seen: set[str] = set()
        for category in self.categories:
            for prefix in category.prefixes:
                if prefix in seen:
                    raise ValueError("a prefix is claimed by two categories")
                seen.add(prefix)
        return self


class ModelBackfillSource(_Frozen):
    """A directory of dated copies read once into the history: ``kind`` names the parser."""

    kind: Literal["floor_simple", "factory_record"]
    glob: str = Field(min_length=1, description="Relative to the registry home.")


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


class ModelFloorCompareRuntime(_Frozen):
    home_env: str
    simple_relpath: str
    record_relpath: str
    history_relpath: str
    output_relpath: str
    overlay_id: str = Field(
        min_length=1,
        description="The overlay that supplies the page's category grouping: <root>/<overlay_id>/overlay.yaml.",
    )
    overlay_file_env: str = Field(
        min_length=1,
        description="Environment variable naming an overlay file that takes the place of the root search.",
    )
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
    details_page_size: int = Field(
        ge=1,
        description="Rows of the page's details table on one page; the page pages through every line of the window.",
    )
    estimated_cost_stoppages: tuple[str, ...] = Field(
        default=(),
        description="Stoppage ids whose logged cost is an estimate of idle capacity, not lost work anything measured; the floor leaves it out, so the history does too.",
    )
    backfill: tuple[ModelBackfillSource, ...] = ()
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
    name: str = Field(
        default="", description="The line's plain-words name, without the numbers."
    )
    unit: str = ""
    category: str = Field(
        default="", description="Id of the category the overlay puts the line in."
    )


class ModelCategoryCard(_Frozen):
    """The roll-up of one category over one window: counts of every line (not only the capped lists) and one sentence."""

    id: str
    title: str
    better: int
    worse: int
    same: int
    unread: int
    total: int
    lean: Literal["better", "worse", "mixed", "same", "unread", "empty"]
    sentence: str
    biggest_change: str = Field(
        description="The one plain sentence of the biggest move, or why there is none."
    )
    biggest_key: str | None = None


class ModelDetailRow(_Frozen):
    """One line of the details table: short, so the whole window can be searched and paged on the page."""

    key: str
    name: str
    category: str
    verdict: Verdict
    old_text: str
    new_text: str
    change_text: str
    size: float | None = Field(
        description="Relative size of the move (absolute change over the old value, at least 1); null when a side is unread."
    )
    better_is: Polarity


class ModelWindowComparison(_Frozen):
    days: int
    label: str
    target: datetime
    better: tuple[ModelCompareLine, ...]
    worse: tuple[ModelCompareLine, ...]
    same: int
    unread: tuple[ModelCompareLine, ...]
    counts: dict[str, int]
    categories: tuple[ModelCategoryCard, ...] = ()
    details: tuple[ModelDetailRow, ...] = Field(
        default=(),
        description="Every line of the window, biggest move first, for the page's searchable and paged table.",
    )


class ModelFloorComparison(_Frozen):
    schema_id: Literal["omni.floor-compare/1"] = "omni.floor-compare/1"
    generated_at: datetime
    history_points: int
    oldest_point: datetime | None
    windows: tuple[ModelWindowComparison, ...]
    details_page_size: int = Field(ge=1)
    categories_error: str = Field(
        default="",
        description="Why the cards are absent (the grouping overlay is missing or invalid); empty when they are drawn.",
    )


class ModelFloorCompareRequest(_Frozen):
    home: str | None = None
    now: datetime | None = None
    drill_error: bool = False
    backfill_only: bool = False
    backfill_history: bool = False
    history_days: int | None = None
    overlay: ModelFloorCompareOverlay | None = None

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
    categories_error: str = ""
