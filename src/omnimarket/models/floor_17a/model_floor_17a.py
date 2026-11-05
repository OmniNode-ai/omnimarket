# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared models of the 17a floor (OMN-19998): emission, receipts, lab state, the simple floor and the alarm.

Everything a deployment decides (the pieces, the joins, the lab sources and their limits, the alarm channels) is an
overlay document validated by the ``Overlay`` models here; no value of that kind is in code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReadingState = Literal["WORKING", "NOT WORKING", "UNKNOWN"]
Phase = Literal["start", "success", "failure"]
Draw = Literal["green", "amber", "red", "grey"]
SourceKind = Literal[
    "hosts", "runner_groups", "disk", "queue_age", "deploy_lag", "broker_lag"
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- the emission contract ---------------------------------------------------------------------------------------------


class ModelNodeEmission(_Frozen):
    """One event on the emission topics: a node's start, its success or its typed failure, with counters."""

    node: str = Field(min_length=1)
    piece: int = Field(ge=1, le=18)
    correlation_id: str = Field(min_length=1)
    phase: Phase
    emitted_at: datetime
    backend: Literal["in_memory", "real"] = "real"
    drill: bool = False
    counters: dict[str, int] = Field(default_factory=dict)
    failure_code: str | None = None
    failure_detail: str = ""

    @model_validator(mode="after")
    def _shape(self) -> ModelNodeEmission:
        if (self.phase == "failure") != bool(self.failure_code):
            raise ValueError(
                "a failure carries a typed failure_code and no other phase does"
            )
        if any(v < 0 for v in self.counters.values()):
            raise ValueError("a counter is never negative")
        return self


class ModelJoinDef(_Frozen):
    """A join between two pieces, as the overlay declares it."""

    id: str = Field(pattern=r"^J(\d{1,2}|-cycle)$")
    from_piece: int = Field(ge=1, le=18)
    to_piece: int = Field(ge=1, le=18)
    label: str = Field(min_length=1)


class ModelPieceDef(_Frozen):
    """One of the 18 pieces, as the overlay declares it."""

    piece: int = Field(ge=1, le=18)
    name: str = Field(min_length=1)
    reading_max_age_s: int = Field(gt=0)
    first_reading: str = ""


class ModelFloorReceiptInput(_Frozen):
    now: datetime
    emissions: tuple[ModelNodeEmission, ...]
    pieces: tuple[ModelPieceDef, ...]
    joins: tuple[ModelJoinDef, ...]
    freshness_max_age_s: int = Field(gt=0)
    error_max_age_s: int = Field(gt=0)
    chain_deadline_s: int = Field(gt=0)


ChainKind = Literal["golden", "error", "open", "broken"]
FindingCode = Literal[
    "success_without_start",
    "failure_without_start",
    "terminal_before_start",
    "success_and_failure",
    "start_without_terminal",
    "duplicate_start",
    "downstream_started_after_upstream_failed",
    "downstream_missing_typed_failure",
    "downstream_started_before_upstream_succeeded",
]


class ModelChainFinding(_Frozen):
    code: FindingCode
    subject: str
    correlation_id: str
    detail: str


class ModelChain(_Frozen):
    piece: int
    node: str
    correlation_id: str
    kind: ChainKind
    started_at: datetime | None
    ended_at: datetime | None
    backend: str
    drill: bool
    counters: dict[str, int] = Field(default_factory=dict)


class ModelPieceReceipt(_Frozen):
    piece: int
    started: bool
    golden_at: datetime | None
    error_at: datetime | None
    golden_backends: tuple[str, ...]
    last_emission_at: datetime | None
    golden_age_s: int | None
    error_age_s: int | None
    freshness: Literal["fresh", "stale", "none"]
    counters: dict[str, int] = Field(default_factory=dict)


class ModelJoinReceipt(_Frozen):
    join: str
    from_piece: int
    to_piece: int
    golden_at: datetime | None
    error_at: datetime | None
    state: Literal["not_run", "golden_only", "error_only", "passed", "broken"]


class ModelFloorReceipts(_Frozen):
    computed_at: datetime
    pieces: tuple[ModelPieceReceipt, ...]
    joins: tuple[ModelJoinReceipt, ...]
    chains: tuple[ModelChain, ...]
    findings: tuple[ModelChainFinding, ...]


# --- the lab state -----------------------------------------------------------------------------------------------------


class ModelLabSourceDef(_Frozen):
    """One lab source, as the overlay declares it: what is read, how old a read may be, and the line it must stay inside."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    kind: SourceKind
    max_age_s: int = Field(gt=0)
    limit: float | None = None
    unit: str = ""
    tickets: tuple[str, ...] = ()


class ModelLabSubject(_Frozen):
    name: str
    ok: bool
    value: float | None = None
    note: str = ""


class ModelLabSourceRead(_Frozen):
    """What the reader got from one source, before judgement. ``status`` unread means nothing could be read."""

    id: str
    status: Literal["read", "failed", "unread"]
    read_at: datetime | None = None
    reason: str = ""
    summary: str = ""
    subjects: tuple[ModelLabSubject, ...] = ()


class ModelLabStateOverlay(_Frozen):
    """The overlay of the lab-state reader and node: the sources and how each is read."""

    sources: tuple[ModelLabSourceDef, ...] = Field(min_length=6)
    disk_keys: tuple[str, ...] = Field(min_length=1)
    broker_conditions: tuple[str, ...] = Field(min_length=1)
    queue_age_classes: tuple[str, ...] = Field(min_length=1)
    deploy_lag_lookback_days: int = Field(gt=0)
    require_runner_version: bool
    declared_host_states: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _one_per_kind(self) -> ModelLabStateOverlay:
        kinds = [s.kind for s in self.sources]
        if len(set(kinds)) != len(kinds):
            raise ValueError("declare each lab source kind once")
        return self


class ModelLabStateInput(_Frozen):
    now: datetime
    sources: tuple[ModelLabSourceDef, ...]
    reads: tuple[ModelLabSourceRead, ...]


class ModelLabSourceVerdict(_Frozen):
    id: str
    title: str
    kind: SourceKind
    state: ReadingState
    probed_at: datetime | None
    published_at: datetime
    age_s: int | None
    max_age_s: int
    reason: str
    summary: str
    subjects: tuple[ModelLabSubject, ...]
    tickets: tuple[str, ...] = ()


class ModelLabStateOutput(_Frozen):
    computed_at: datetime
    verdicts: tuple[ModelLabSourceVerdict, ...]


# --- the simple floor --------------------------------------------------------------------------------------------------


class ModelFloorNumber(_Frozen):
    key: Literal["merges", "open_prs", "slots"]
    value: str
    label: str
    basis: str
    read_at: datetime | None
    read: bool


class ModelFloorLane(_Frozen):
    """A lane as the record draws it: the only facts the red rule needs."""

    lane: str
    ticket: str = ""
    stalled: bool = False


class ModelFloorCauseIn(_Frozen):
    """A candidate cause, before the rule that decides its colour."""

    id: str
    title: str
    detail: str
    size: int = Field(ge=0)
    size_unit: str
    tickets: tuple[str, ...] = ()
    needs_operator: bool = False


class ModelFloorCause(_Frozen):
    id: str
    title: str
    detail: str
    size: int
    size_unit: str
    state: Literal["red", "amber"]
    work_under_way: bool
    needs_operator: bool
    lanes: tuple[str, ...] = ()
    tickets: tuple[str, ...] = ()
    listed: bool = True
    members: tuple[str, ...] = ()


class ModelFloorJoinRow(_Frozen):
    id: str
    label: str
    to_piece: int
    state: str
    golden_age_s: int | None
    error_age_s: int | None


class ModelFloorPieceRow(_Frozen):
    piece: int
    name: str
    status: Literal["not_started", "started", "working", "not_working", "unknown"]
    draw: Draw
    text: str
    reading_state: ReadingState | None
    reading_age_s: int | None
    reading_max_age_s: int
    golden_age_s: int | None
    error_age_s: int | None
    freshness: Literal["fresh", "stale", "none"]
    cause_id: str | None = None
    joins: tuple[ModelFloorJoinRow, ...] = ()


class ModelFloorLabLine(_Frozen):
    id: str
    title: str
    state: ReadingState
    draw: Draw
    text: str
    reading_age_s: int | None
    max_age_s: int
    work_under_way: bool
    cause_id: str | None = None


class ModelFloorAlarmLine(_Frozen):
    alarm_id: str
    source_id: str
    title: str
    raised_at: datetime
    delivery: Literal["pending", "delivered", "undelivered"]
    channel: str


class ModelRebuild(_Frozen):
    current_piece: int
    headline: str
    next_text: str
    go_given_through: int


class ModelRedRule(_Frozen):
    """The reading of the rule: a red row never has work under way on its cause."""

    state: ReadingState
    red_rows: int
    red_with_work_under_way: int
    violations: tuple[str, ...] = ()


class ModelFloorSimple(_Frozen):
    schema_id: Literal["omni.floor-simple/1"] = "omni.floor-simple/1"
    generated_at: datetime
    freshness_max_age_s: int
    level: Draw
    sentence: str
    subline: str
    numbers: tuple[ModelFloorNumber, ...]
    causes: tuple[ModelFloorCause, ...]
    causes_shown: int
    rebuild: ModelRebuild
    pieces: tuple[ModelFloorPieceRow, ...]
    lab_state: tuple[ModelFloorLabLine, ...]
    alarms: tuple[ModelFloorAlarmLine, ...]
    red_rule: ModelRedRule
    chain_findings: tuple[ModelChainFinding, ...] = ()
    details_href: str
    sources_read: dict[str, bool] = Field(default_factory=dict)
    alarm_heartbeat_at: datetime | None = None


class ModelRebuildOverlay(_Frozen):
    go_given_through_piece: int = Field(ge=0, le=18)
    current_piece: int = Field(ge=1, le=18)
    step0_pieces: tuple[int, ...] = ()
    lab_state_piece: int = Field(ge=1, le=18)


class ModelNumbersOverlay(_Frozen):
    landing_goal_per_hour: int = Field(gt=0)
    merge_window_minutes: int = Field(gt=0)
    excluded_pr_classes: tuple[str, ...] = ()
    watcher_max_age_s: int = Field(gt=0)
    record_max_age_s: int = Field(gt=0)


class ModelCauseOverlay(_Frozen):
    max_shown: int = Field(gt=0)
    titles: dict[str, str] = Field(default_factory=dict)
    owners: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    needs_operator: tuple[str, ...] = ()
    ignore_stoppages: tuple[str, ...] = ()
    pr_classes: dict[str, str] = Field(default_factory=dict)


class ModelFloorSimpleOverlay(_Frozen):
    """The overlay of node_floor_simple_compute: the pieces, the joins, the numbers' goal and the cause wording."""

    pieces: tuple[ModelPieceDef, ...] = Field(min_length=18, max_length=18)
    joins: tuple[ModelJoinDef, ...] = Field(min_length=1)
    rebuild: ModelRebuildOverlay
    numbers: ModelNumbersOverlay
    causes: ModelCauseOverlay
    freshness_max_age_s: int = Field(gt=0)
    error_receipt_max_age_s: int = Field(gt=0)
    error_drill_after_s: int = Field(gt=0)
    chain_deadline_s: int = Field(gt=0)
    details_href: str = Field(min_length=1)

    @model_validator(mode="after")
    def _eighteen(self) -> ModelFloorSimpleOverlay:
        if sorted(p.piece for p in self.pieces) != list(range(1, 19)):
            raise ValueError("the overlay declares each of the pieces 1 to 18 once")
        return self


class ModelReadingRow(_Frozen):
    """One reading row for a piece, as the reading writer node publishes it."""

    piece: int
    state: ReadingState
    probed_at: datetime
    published_at: datetime
    max_age_s: int


class ModelFloorSimpleInput(_Frozen):
    now: datetime
    overlay: ModelFloorSimpleOverlay
    open_prs: int | None
    open_prs_before: int | None
    merges_in_window: int | None
    watcher_read_at: datetime | None
    slots_working: int | None
    slots_total: int | None
    slots_unread_hosts: tuple[str, ...] = ()
    record_read_at: datetime | None
    pr_class_counts: dict[str, int] = Field(default_factory=dict)
    pr_class_tickets: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    stoppages: tuple[ModelFloorCauseIn, ...] = ()
    lanes: tuple[ModelFloorLane, ...] = ()
    lab: ModelLabStateOutput | None
    receipts: ModelFloorReceipts | None
    readings: dict[int, ModelReadingRow] | None = None
    alarms: tuple[ModelFloorAlarmLine, ...] = ()
    sources_read: dict[str, bool] = Field(default_factory=dict)
    alarm_heartbeat_at: datetime | None = None


# --- the alarm ---------------------------------------------------------------------------------------------------------


class ModelAlarmChannelDef(_Frozen):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["unconfigured", "in_memory", "floor_banner"]
    role: Literal["primary", "second"]


class ModelAlarmOverlay(_Frozen):
    readback_s: int = Field(gt=0)
    renotify_s: int = Field(gt=0)
    heartbeat_max_age_s: int = Field(gt=0)
    channels: tuple[ModelAlarmChannelDef, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _roles(self) -> ModelAlarmOverlay:
        roles = [c.role for c in self.channels]
        if roles.count("primary") != 1 or roles.count("second") != 1:
            raise ValueError("declare exactly one primary and one second channel")
        return self


class ModelAlarm(_Frozen):
    alarm_id: str
    source_id: str
    title: str
    detail: str
    raised_at: datetime


class ModelAlarmSend(_Frozen):
    alarm_id: str
    channel: str
    role: Literal["primary", "second"]
    receipt_id: str | None
    sent_at: datetime
    delivery: Literal["pending", "delivered", "failed"]
    detail: str = ""
    read_back_at: datetime | None = None


class ModelOpenAlarm(_Frozen):
    alarm: ModelAlarm
    sends: tuple[ModelAlarmSend, ...] = ()
    escalated: bool = False
    last_notified_at: datetime


class ModelAlarmState(_Frozen):
    open: dict[str, ModelOpenAlarm] = Field(default_factory=dict)
    heartbeat_at: datetime | None = None


class ModelLabStateAlarmRequest(_Frozen):
    now: datetime
    verdicts: tuple[ModelLabSourceVerdict, ...]
    state: ModelAlarmState
    overlay: ModelAlarmOverlay


class ModelLabStateAlarmResult(_Frozen):
    state: ModelAlarmState
    raised: tuple[str, ...]
    escalated: tuple[str, ...]
    cleared: tuple[str, ...]
    undelivered: tuple[str, ...]


class ModelCanaryInput(_Frozen):
    now: datetime
    record_generated_at: datetime | None
    heartbeat_at: datetime | None
    floor_max_age_s: int = Field(gt=0)
    heartbeat_max_age_s: int = Field(gt=0)


class ModelCanaryVerdict(_Frozen):
    state: Literal["ok", "floor_silent", "alarm_silent", "both_silent"]
    notify: bool
    message: str
    floor_age_s: int | None
    heartbeat_age_s: int | None


# --- the tick ----------------------------------------------------------------------------------------------------------


class ModelFloorSimpleTickRequest(_Frozen):
    """The flags of one run of the 17a tick."""

    home: str | None = None
    now: datetime | None = None
    drill_error: bool = False
    check_overlays_root: str | None = None


class ModelFloorSimpleTickResult(_Frozen):
    ok: bool
    correlation_id: str
    generated_at: datetime
    output: str | None
    level: Draw | None
    red_rule: ReadingState | None
    failure_code: str | None = None
    failure_detail: str = ""
    emitted: int = 0
