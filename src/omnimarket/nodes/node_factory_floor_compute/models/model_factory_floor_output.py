# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor record: one line per selected plan and the floor section."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_factory_floor_input import (
    ItemState,
)
from omnimarket.models.model_floor_phoenix_traces import ModelFloorPhoenixTraces
from omnimarket.models.model_merged_running_count import (
    ModelMergedRunningCountResult,
)

Condition = Literal["queued", "building", "stopped", "complete", "needs_review"]
ProgressState = Literal["known", "unknown", "no_tasks"]
GateOutcome = Literal["passed", "failed", "waiting"]
# What backs a done task: a merged pull request, a done lane TERMINAL, a closed Linear issue, or only a recorded state.
# None of these is deployment proof; "" is a task with no done evidence.
EvidenceKind = Literal["merged", "terminal", "linear_done", "recorded", ""]
CiOut = Literal["green", "red", "pending", "none", "unread", ""]
LineSource = Literal["manifest", "plan-task-table", "undrawn"]


class ModelFloorItem(BaseModel):
    """One part (a task) at a station."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str
    kind: str
    state: ItemState
    note: str
    tickets: tuple[str, ...]
    prs: tuple[str, ...]
    # What backs the item's state, so a reader can say what a "done" rests on without implying it is deployed.
    evidence_kind: EvidenceKind = ""


class ModelFloorStation(BaseModel):
    """One station: a task of a plan (or, under wave grouping, a wave of tasks)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    state: ItemState
    items: tuple[ModelFloorItem, ...]
    feeders: tuple[str, ...]
    # The wave label of the station's task ("" when the plan declares none): the band a renderer groups stations by.
    group: str = ""
    # The tickets of the station's own parts.
    tickets: tuple[str, ...] = ()
    # The pull requests of the station's own parts.
    prs: tuple[str, ...] = ()


class ModelFloorAndon(BaseModel):
    """The andon: an open andon hold or a failed gate stops the line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flag: bool
    reason: str
    causes: tuple[str, ...]
    # True when a hold, a failed gate or a plan with no tasks stops the line whatever runs on it. False when every cause
    # is a red pull request or a stop the ledger recorded, which a running lane on the line is already working through.
    hard: bool = False


class ModelFloorLaneContainers(BaseModel):
    """One lane's application containers on one host against its declared budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    lane: str
    status: Literal["PASS", "FAIL", "UNKNOWN"]
    observed: int | None
    budget: int | None
    alarm_key: str
    # The read's own sentence: the count against the budget, or why the host could not be read (UNKNOWN).
    detail: str = ""


class ModelFloorApplicationContainers(BaseModel):
    """The container read shown on the selected line: per host and lane, or ``unread`` when no host was read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["read", "unread"]
    lanes: tuple[ModelFloorLaneContainers, ...]


class ModelFloorBlocker(BaseModel):
    """Why a line is not moving: the first cause as a sentence and the refs that show it (task ids, PR keys, ledger rows)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    evidence: tuple[str, ...]


class ModelFloorProgress(BaseModel):
    """Work tasks complete over valid work tasks. Gates are never in it; unknown is never 0."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ProgressState
    completed: int | None
    total: int | None
    # completed / total x 100, rounded half-up to one decimal; None unless state is known.
    percent: float | None
    note: str


class ModelFloorGateRef(BaseModel):
    """One gate of the line and where it stands."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    state: GateOutcome


class ModelFloorGates(BaseModel):
    """The line's gates, counted apart from its work tasks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: int
    failed: int
    waiting: int
    items: tuple[ModelFloorGateRef, ...]


class ModelFloorLineEvidence(BaseModel):
    """Counts over the work tasks, one per kind of evidence; None means that source was not read, never 0."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    merged: int
    running_on_dev: int | None
    verified: int | None
    of: int


class ModelFloorDataAge(BaseModel):
    """The oldest source read the line depends on, and its age against the record's own time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_at: str
    age_min: int | None


class ModelFloorLine(BaseModel):
    """One car on the line: a selected plan, or a declared line no plan could be read for.

    For ``source`` undrawn there is no plan: ``plan_path`` holds the declared text itself, so a reader that pairs a
    declared text with the line whose path contains it pairs this line, and the andon carries the cause.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: str
    name: str
    plan_path: str
    source: LineSource
    # The one condition of the line, read by every surface. A blocker never recolours or erases earned progress.
    condition: Condition
    blocker: ModelFloorBlocker | None
    progress: ModelFloorProgress
    gates: ModelFloorGates
    line_evidence: ModelFloorLineEvidence
    data_age: ModelFloorDataAge
    stations: tuple[ModelFloorStation, ...]
    # The stop facts behind the blocker; it no longer drives the condition.
    andon: ModelFloorAndon
    # Set on the line the selection names for the container read; every other line carries None.
    application_containers: ModelFloorApplicationContainers | None = None


class ModelFloorBay(BaseModel):
    """One lab host's bays: lanes running, bays free, and whether its advertisement is stale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    slots: int
    running: int
    free: int
    stale: bool


class ModelFloorPrRef(BaseModel):
    """A pull request on the floor, shipped (merged) or waiting, and the line it belongs to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str
    title: str
    line_id: str
    detail: str


class ModelFloorStoppageOut(BaseModel):
    """A friction class standing on the floor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    occurrences: int
    cost_minutes: float
    recurring: bool
    tickets: tuple[str, ...]
    last_seen: str


MachineState = Literal["read", "unread", "repair", "out"]
SourceState = Literal["read", "unread"]
LaneTitleSource = Literal["linear", "claim", "unread"]
LanePlacement = Literal["machine", "cloud", "unplaced"]
# Why a lane the floor cannot place is unplaced: no row names a host, or the host named is not a drawn machine.
PlacementCause = Literal["", "no-host-cell", "host-not-drawn"]


class ModelFloorRunnersOut(BaseModel):
    """The CI runners a machine carries, or why that is unread."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: SourceState
    total: int | None
    online: int | None
    busy: int | None
    observed_at: str
    note: str


AdmissionState = Literal["open", "refused", "unread"]


class ModelFloorAdmission(BaseModel):
    """Whether the tick's admission takes new work on a machine, and the reading's own words when it does not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: AdmissionState
    reason: str


class ModelFloorMachine(BaseModel):
    """One machine bay: what the pool read saw, the lanes placed on it against its cap, its units and CI runners."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    label: str
    state: MachineState
    note: str
    read_at: str
    read_age_min: int | None
    stale: bool
    cores: int | None
    load1: float | None
    load_per_core: float | None
    busy_cores: float | None
    mem_free_gb: float | None
    mem_total_gb: float | None
    swap_used_pct: float | None
    cap: int | None
    refusal: str
    free_slots: int
    admission: ModelFloorAdmission
    lanes_placed: int
    lanes: tuple[str, ...]
    units_placed: int | None
    unit_work: int | None
    runners: ModelFloorRunnersOut


class ModelFloorStationRef(BaseModel):
    """The station (and part) of an assembly line a lane builds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: str
    station_id: str
    item_id: str


class ModelFloorLane(BaseModel):
    """One live lane: what it is for, what it did last, its PR and how long it has run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    ticket: str
    title: str
    title_source: LaneTitleSource
    kind: str
    engine: str
    placement: LanePlacement
    host: str
    # Set only on an unplaced lane: the reason it names no drawn machine, and the host as written when it named one.
    placement_cause: PlacementCause = ""
    named_host: str = ""
    cloud_account: str
    started_at: str
    run_minutes: int
    status_text: str
    status_full: str
    status_at: str
    status_age_min: int | None
    status_source: Literal["status", "claim"]
    silent_minutes: int
    stalled: bool
    pr: str
    pr_title: str
    pr_state: str
    pr_ci: CiOut
    pr_ci_note: str
    station: ModelFloorStationRef | None
    claim_full: str


class ModelFloorLamp(BaseModel):
    """A stop lamp on the cloud bay: something that blocks the cloud sessions, with the receipt that shows it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    text: str
    since: str
    evidence: str


class ModelFloorCloudAccountOut(BaseModel):
    """One cloud account: whether it is signed in, its session cap and how many of its listed sessions are open (None when the receipts could not be read)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    ready: bool
    login: str
    cap: int | None
    open_sessions: int | None
    record_note: str
    credit_remaining: float | None
    credit_read_at: str
    rate_status: str
    rate_resets_at: str


class ModelFloorCloudSession(BaseModel):
    """One cloud session (a receipt of a lane placed in the cloud)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    run_id: str
    account: str
    started_at: str
    run_minutes: int
    status: str
    open: bool
    result: str


class ModelFloorCloud(BaseModel):
    """The cloud bay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: SourceState
    note: str
    accounts: tuple[ModelFloorCloudAccountOut, ...]
    sessions: tuple[ModelFloorCloudSession, ...]
    lamps: tuple[ModelFloorLamp, ...]


class ModelFloorSection(BaseModel):
    """The floor around the lines."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    machines: tuple[ModelFloorMachine, ...]
    cloud: ModelFloorCloud
    lanes: tuple[ModelFloorLane, ...]
    stall_minutes: int
    lane_window_minutes: int
    bays: tuple[ModelFloorBay, ...]
    shipped: tuple[ModelFloorPrRef, ...]
    waiting: tuple[ModelFloorPrRef, ...]
    stoppages: tuple[ModelFloorStoppageOut, ...]
    phoenix_traces: ModelFloorPhoenixTraces
    # The merged-and-running count for the Eastern day and the idle slots per host; None when it was not read.
    merged_running: ModelMergedRunningCountResult | None = None


class ModelFloorSources(BaseModel):
    """What the record was computed from, so a reader can judge its freshness."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    watcher_last_tick: str
    ledger_rows: int
    ledger_newest_row: str
    linear_issues: int
    placement_hosts: int
    stoppage_report: bool
    pool_read_at: str
    host_table_read: bool
    placement_markers_read: bool
    receipts_read: bool
    cloud_accounts_read: bool
    runner_fleet_read: bool
    # Why the runner fleet is unread, in the reader's words; empty when it was read.
    runner_fleet_cause: str = ""
    # Why the merged-and-running count is unread, in the reader's words; empty when it was read.
    merged_running_cause: str = ""


class ModelFactoryFloorResult(BaseModel):
    """The factory floor record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str
    generated_at: str
    lines: tuple[ModelFloorLine, ...]
    floor: ModelFloorSection
    sources: ModelFloorSources
