# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and output of the process-health series.

Every input is a fact the caller already read: a source read (status, coverage, why unread) and the
typed records that source held. The node reads no file, no network and no clock. The output is one
metric list per UTC day, and the same list over the equal-length windows on both sides of the
factory reset, so a number in the summary is the number the daily series would give for that span.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow


class EnumReadStatus(StrEnum):
    """Whether a source covered a window."""

    READ = "read"
    PARTIAL = "partial"
    UNREAD = "unread"


class EnumSourceName(StrEnum):
    """The sources a metric can name."""

    LEDGER = "ledger"
    LANDING_DECISIONS = "landing_decisions"
    LANDING_COST = "landing_cost"
    LANDING_DELEGATION = "landing_delegation"
    LANDING_METRICS_HISTORY = "landing_metrics_history"
    REPO_STATUS_EVENTS = "repo_status_events"
    GIT_MERGES = "git_merges"
    LINEAR_SNAPSHOT = "linear_snapshot"


class EnumMetricKind(StrEnum):
    """How a metric is made comparable across windows of different length."""

    COUNT = "count"
    HOURS = "hours"
    MINUTES = "minutes"
    RATIO = "ratio"
    MEDIAN_MINUTES = "median_minutes"


class EnumPolarity(StrEnum):
    """Which direction is an improvement."""

    LOWER_IS_BETTER = "lower_is_better"
    HIGHER_IS_BETTER = "higher_is_better"
    NEUTRAL = "neutral"


class EnumVerdict(StrEnum):
    """The before and after reading of one metric."""

    IMPROVED = "improved"
    WORSE = "worse"
    FLAT = "flat"
    UNMEASURED = "unmeasured"
    INFORMATIONAL = "informational"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value


class ModelTimeSpan(_Frozen):
    """A span inside a source's coverage that the source did not record."""

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _aware(value)


class ModelSourceRead(_Frozen):
    """One source as the caller read it: unread with a reason, or read over a covered span."""

    name: EnumSourceName
    status: EnumReadStatus
    locator: str = Field(description="Where it was read, with the home directory as ~.")
    reason: str = Field(default="", description="Why the source is unread or partial.")
    covered_from: datetime | None = None
    covered_to: datetime | None = None
    gaps: tuple[ModelTimeSpan, ...] = ()

    @field_validator("covered_from", "covered_to")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.status is EnumReadStatus.UNREAD:
            if not self.reason:
                raise ValueError("an unread source must say why")
            return self
        if self.covered_from is None or self.covered_to is None:
            raise ValueError("a read source must declare covered_from and covered_to")
        if self.covered_from > self.covered_to:
            raise ValueError("covered_from is after covered_to")
        return self


class ModelRepoStatusEvent(_Frozen):
    """One repo-status verdict change from the throughput tick."""

    at: datetime
    repo: str
    verdict: str
    check: str | None = None

    @field_validator("at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _aware(value)


class ModelMergeRecord(_Frozen):
    """One first-parent squash commit on a default branch."""

    repo: str
    number: int | None
    committed_at: datetime
    author: str
    subject: str
    docs_only: bool
    ledger_only: bool

    @field_validator("committed_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _aware(value)


class ModelControllerDecision(_Frozen):
    """One landing controller decision row about a pull request (``repo#number``)."""

    ts: datetime
    pr: str
    head: str
    kind: str
    outcome: str

    @field_validator("ts")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _aware(value)


class ModelControllerLease(_Frozen):
    """One released fix-worker lease from the controller cost file."""

    kind: str
    subject: str
    repo: str
    spawned_at: datetime
    released_at: datetime
    worker_minutes: float

    @field_validator("spawned_at", "released_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _aware(value)


class ModelDelegationRow(_Frozen):
    """One controller delegation row: a lease and how many delegated runs it made."""

    ts: datetime
    lease_id: int
    delegated: int

    @field_validator("ts")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _aware(value)


class ModelLandingMetricsWindow(_Frozen):
    """One record of the landing metrics history (a measured window)."""

    window_end: datetime
    green_to_merged_n: int
    median_green_to_merged_min: float | None

    @field_validator("window_end")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _aware(value)


class ModelLinearIssue(_Frozen):
    """One Linear issue of the snapshot."""

    identifier: str
    created_at: datetime
    completed_at: datetime | None = None
    canceled_at: datetime | None = None
    labels: tuple[str, ...] = ()
    history_read: bool = Field(
        default=False,
        description="True when the snapshot holds this issue's state history.",
    )
    reopened_at: tuple[datetime, ...] = ()

    @field_validator("created_at", "completed_at", "canceled_at")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)


class ModelLinearSnapshot(_Frozen):
    """The Linear snapshot file: the issues a read returned and the span that read covered."""

    covered_from: datetime
    covered_to: datetime
    issues: tuple[ModelLinearIssue, ...]

    @field_validator("covered_from", "covered_to")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _aware(value)


class ModelProcessHealthConfig(_Frozen):
    """Every threshold, pattern and name the metrics use; declared in the node contract."""

    reset_at: datetime
    reset_label: str
    series_days: int = Field(gt=0)
    summary_max_hours: float = Field(gt=0)
    flat_threshold_ratio: float = Field(ge=0)
    fix_forward_window_hours: float = Field(gt=0)
    fix_subject_pattern: str
    revert_subject_pattern: str
    ticket_pattern: str
    umbrella_min_prs: int = Field(gt=0)
    bot_markers: tuple[str, ...]
    ledger_roll_subject_pattern: str
    reporter_actor_prefixes: tuple[str, ...]
    capacity_estimate_classes: tuple[str, ...]
    estimate_cost_prefixes: tuple[str, ...]
    preflight_guard_marker: str
    refusal_class: str
    unclassified_label: str
    lane_outcome_classes: dict[str, tuple[str, ...]]
    other_outcome_class: str
    landing_lane_prefix: str
    landing_failed_results: tuple[str, ...]
    landing_failed_class: str
    red_verdict: str
    fix_dispatch_kind: str
    fix_dispatch_outcome: str
    andon_reason_prefixes: tuple[str, ...]
    andon_text_prefixes: tuple[str, ...]
    bug_labels: tuple[str, ...]
    relapse_min_gap_minutes: float = Field(ge=0)

    @field_validator("reset_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _aware(value)


class ModelProcessHealthRequest(_Frozen):
    """All inputs; the node has no clock, so the generated-at time is the last day's end."""

    generated_at: datetime
    config: ModelProcessHealthConfig
    sources: tuple[ModelSourceRead, ...]
    ledger_rows: tuple[ModelLandingLedgerRow, ...] = ()
    repo_status_events: tuple[ModelRepoStatusEvent, ...] = ()
    merges: tuple[ModelMergeRecord, ...] = ()
    decisions: tuple[ModelControllerDecision, ...] = ()
    leases: tuple[ModelControllerLease, ...] = ()
    delegation_rows: tuple[ModelDelegationRow, ...] = ()
    landing_metrics: tuple[ModelLandingMetricsWindow, ...] = ()
    linear_issues: tuple[ModelLinearIssue, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def _unique_sources(self) -> Self:
        names = [source.name for source in self.sources]
        if len(set(names)) != len(names):
            raise ValueError("a source may be declared once")
        return self


class ModelBreakdownItem(_Frozen):
    """One key of a metric's breakdown (a repo, a class, a kind)."""

    key: str
    value: float


class ModelMetricSource(_Frozen):
    """A source a metric used and how much of the metric's window it covered."""

    name: EnumSourceName
    status: EnumReadStatus
    covered_hours: float
    reason: str = ""


class ModelMetric(_Frozen):
    """One number over one window, with its sources, its method and a breakdown."""

    key: str
    label: str
    unit: str
    kind: EnumMetricKind
    polarity: EnumPolarity
    value: float | None
    status: EnumReadStatus
    sources: tuple[ModelMetricSource, ...]
    method: str
    note: str = ""
    breakdown: tuple[ModelBreakdownItem, ...] = ()


class ModelWindowMetrics(_Frozen):
    """The metric list of one window."""

    start: datetime
    end: datetime
    hours: float
    metrics: tuple[ModelMetric, ...]


class ModelDaySeries(_Frozen):
    """One UTC day of the series."""

    date: str
    is_reset_day: bool
    partial: bool = Field(
        description="True for the day that ends at the generated-at time."
    )
    window: ModelWindowMetrics


class ModelMetricComparison(_Frozen):
    """One metric before and after the reset, made comparable by day or as a ratio."""

    key: str
    label: str
    unit: str
    kind: EnumMetricKind
    polarity: EnumPolarity
    before: float | None
    after: float | None
    delta: float | None
    delta_ratio: float | None
    verdict: EnumVerdict
    before_status: EnumReadStatus
    after_status: EnumReadStatus
    comparable_unit: str
    note: str = ""


class ModelResetSummary(_Frozen):
    """The equal-length windows around the reset and each metric compared across them."""

    reset_at: datetime
    reset_label: str
    measured: bool
    reason: str
    window_hours: float
    before: ModelWindowMetrics | None
    after: ModelWindowMetrics | None
    comparisons: tuple[ModelMetricComparison, ...]
    improved: int
    worse: int
    flat: int
    unmeasured: int


class ModelSourceReport(_Frozen):
    """A source as declared by the caller, echoed so the page can show what was and was not read."""

    name: EnumSourceName
    status: EnumReadStatus
    locator: str
    reason: str
    covered_from: datetime | None
    covered_to: datetime | None
    gap_hours: float
    records: int


class ModelProcessHealthResult(_Frozen):
    """The daily series, the reset marker and the before and after summary."""

    schema_version: int
    generated_at: datetime
    reset_at: datetime
    reset_label: str
    sources: tuple[ModelSourceReport, ...]
    days: tuple[ModelDaySeries, ...]
    summary: ModelResetSummary
