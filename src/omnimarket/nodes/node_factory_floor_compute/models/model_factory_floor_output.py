# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor record: one line per selected plan and the floor section."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_factory_floor_input import (
    ItemState,
)

StatusPill = Literal["stopped", "done", "building", "queued"]
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


class ModelFloorStation(BaseModel):
    """One station: a wave or stage of a plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    state: ItemState
    items: tuple[ModelFloorItem, ...]
    feeders: tuple[str, ...]


class ModelFloorAndon(BaseModel):
    """The andon: an open andon hold or a failed gate stops the line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flag: bool
    reason: str
    causes: tuple[str, ...]


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
    status_pill: StatusPill
    percent_built: int
    stations: tuple[ModelFloorStation, ...]
    andon: ModelFloorAndon


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


class ModelFloorRunnersOut(BaseModel):
    """The CI runners a machine carries, or why that is unread."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: SourceState
    total: int | None
    online: int | None
    busy: int | None
    observed_at: str
    note: str


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
    """One cloud account: whether it is signed in, its session cap and how many sessions are open."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    ready: bool
    login: str
    cap: int | None
    open_sessions: int
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


class ModelFactoryFloorResult(BaseModel):
    """The factory floor record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str
    generated_at: str
    lines: tuple[ModelFloorLine, ...]
    floor: ModelFloorSection
    sources: ModelFloorSources
