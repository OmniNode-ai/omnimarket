# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared factory floor facts: the typed input of node_factory_floor_compute.

node_factory_floor_effect reads the sources into these models and calls the compute node with them, so
they live here and not in either node's package (no cross-node model reach-in). Every source is data the
caller already read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_lane_container_budget import (
    ModelLaneContainerBudgetVerdict,
)

ItemState = Literal["done", "active", "prep", "wait", "stop"]
TableTaskState = Literal["merged", "open-green", "open-red", "not-started", "unread"]
PrState = Literal["OPEN", "MERGED", "CLOSED"]
CiState = Literal["green", "red", "pending", "none", "unread"]


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must carry a UTC offset")
    return value


class ModelFloorManifestTask(BaseModel):
    """One row of a plan's ``.tasks.yaml``; keys the node does not read are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1)
    title: str = ""
    repo: str = ""
    wave: str = ""
    kind: str = "lane"
    depends_on: tuple[str, ...] = ()
    tickets: tuple[str, ...] = ()
    prs: tuple[str, ...] = ()
    # True for a task read from a plan's own paragraphs: its id (T1, S3) is not unique across plans, so only its
    # tickets and pull requests link it to the ledger and the watcher, never the bare id.
    scoped: bool = False


class ModelFloorManifest(BaseModel):
    """The task manifest beside a plan."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str = ""
    epic: str = ""
    tasks: tuple[ModelFloorManifestTask, ...]


class ModelFloorTableTask(BaseModel):
    """One row of the plans-today task table, the fallback when a plan has no manifest."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    label: str = Field(min_length=1)
    text: str = ""
    tickets: tuple[str, ...] = ()
    prs: tuple[str, ...] = ()
    state: TableTaskState
    state_basis: str = ""
    owner: str = ""


class ModelFloorPlanInput(BaseModel):
    """One plan: its identity, its manifest when it has one, its plans-today table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path: str = Field(min_length=1)
    title: str = ""
    tickets: tuple[str, ...] = ()
    prs: tuple[str, ...] = ()
    manifest: ModelFloorManifest | None = None
    table_tasks: tuple[ModelFloorTableTask, ...] = ()


class ModelFloorPr(BaseModel):
    """One pull request as the PR watcher records it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    state: PrState
    draft: bool = False
    red: bool = False
    title: str = ""
    head_ref: str = ""
    tickets: tuple[str, ...] = ()
    ci: CiState = "unread"
    ci_note: str = ""


class ModelFloorMerge(BaseModel):
    """One merge, from the watcher's merge record (the landing controller's merges among them)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    merged_at: datetime
    title: str = ""
    head_ref: str = ""
    tickets: tuple[str, ...] = ()

    _utc = field_validator("merged_at")(_aware)


class ModelFloorWatcher(BaseModel):
    """The PR watcher state: open and merged pull requests and the merge record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    last_tick: datetime
    prs: tuple[ModelFloorPr, ...] = ()
    merges: tuple[ModelFloorMerge, ...] = ()

    _utc = field_validator("last_tick")(_aware)


class ModelFloorLedgerRow(BaseModel):
    """The cells of one ledger row the floor reads (CLAIM, TERMINAL, HOLD, RELEASE, gate STATUS)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime
    kind: str = Field(min_length=1)
    lane: str = ""
    tickets: tuple[str, ...] = ()
    prs: tuple[str, ...] = ()
    row_id: str = ""
    re: str = ""
    until: datetime | None = None
    reason: str = ""
    task: str = ""
    state: str = ""
    subject: str = ""
    verdict: str = ""
    outcome: str = ""
    closes_claim: str = ""
    text: str = ""
    host: str = ""
    model: str = ""
    actor: str = ""
    to: str = ""

    _utc = field_validator("ts")(_aware)


class ModelFloorLinearIssue(BaseModel):
    """One issue of the Linear snapshot plans-today writes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    status: str = ""
    status_type: str = ""
    title: str = ""


class ModelFloorStoppage(BaseModel):
    """One friction class from the friction report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    occurrences: int = Field(ge=0)
    cost_minutes: float = Field(ge=0.0)
    recurring: bool = False
    tickets: tuple[str, ...] = ()
    last_seen: str = ""


class ModelFloorSelection(BaseModel):
    """Which plans are lines: a plan whose path contains any text; empty selects every plan given."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path_contains: tuple[str, ...] = ()
    # The declared line (a plan_path_contains text) that shows each lane's application containers; empty shows none.
    container_read_line: str = ""


class ModelFloorMachineDecl(BaseModel):
    """A machine the deployment overlay shows as a bay. A machine not declared here is never drawn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    label: str = Field(min_length=1)
    runner_labels: tuple[str, ...] = ()


class ModelFloorPoolReading(BaseModel):
    """One host as the landing placement's pool read reported it (the fields the bay draws)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    read_at: datetime
    error: str = ""
    cores: int | None = None
    load1: float | None = None
    busy_cores: float | None = None
    mem_free_gb: float | None = None
    mem_total_gb: float | None = None
    swap_used_pct: float | None = None
    cap: int | None = None
    slots: int | None = None
    refusal: str = ""

    _utc = field_validator("read_at")(_aware)


class ModelFloorHostRow(BaseModel):
    """One row of the shared lab host table: its cap and whether the host is out of selection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    cap: int | None = None
    available: bool = True
    unavailable_reason: str = ""


class ModelFloorPlacedHost(BaseModel):
    """Live placement markers of one host: every placed unit, and of those the remote lanes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    units: int = Field(ge=0)
    lanes: int = Field(ge=0)


class ModelFloorReceipt(BaseModel):
    """The newest remote-lane receipt of one lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    run_id: str = ""
    host: str = ""
    status: str = ""
    final: bool = False
    started_at: datetime
    ended_at: datetime | None = None
    model: str = ""
    ticket: str = ""
    placement: str = ""
    account: str = ""
    pr_heads: tuple[str, ...] = ()
    result: str = ""
    session_detail: str = ""
    session_needs: str = ""

    _utc = field_validator("started_at")(_aware)


class ModelFloorCloudAccount(BaseModel):
    """One cloud account of the cloud pool file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    login: str = ""
    cap: int | None = None
    active: tuple[str, ...] = ()
    credit_remaining: float | None = None
    credit_read_at: str = ""
    rate_status: str = ""
    rate_resets_at: str = ""


class ModelFloorRunner(BaseModel):
    """One CI runner of the runner fleet observation: the host label it carries and its status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_label: str = Field(min_length=1)
    status: Literal["online", "offline", "busy"]


class ModelFloorRunnerFleet(BaseModel):
    """The runner fleet observation the runner monitor builds, reduced to what a bay draws."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at: datetime
    runners: tuple[ModelFloorRunner, ...] = ()

    _utc = field_validator("observed_at")(_aware)


class ModelFloorLaneKindRule(BaseModel):
    """First rule whose any marker is found (case-insensitive) in the lane's actor, name or CLAIM text names its kind."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    actor_contains: tuple[str, ...] = ()
    lane_contains: tuple[str, ...] = ()
    text_contains: tuple[str, ...] = ()


class ModelFactoryFloorConfig(BaseModel):
    """The vocabularies and limits the contract's ``config`` block declares; the handler holds none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str = Field(min_length=1)
    andon_markers: tuple[str, ...] = Field(min_length=1)
    gate_kind: str = Field(min_length=1)
    gate_state: str = Field(min_length=1)
    gate_fail_verdicts: tuple[str, ...] = Field(min_length=1)
    gate_pass_verdicts: tuple[str, ...] = Field(min_length=1)
    done_outcomes: tuple[str, ...] = Field(min_length=1)
    blocked_outcomes: tuple[str, ...] = Field(min_length=1)
    live_claim_minutes: int = Field(gt=0)
    advertisement_stale_factor: int = Field(gt=0)
    station_name_template: str = Field(min_length=1)
    fallback_station_id: str = Field(min_length=1)
    fallback_station_name: str = Field(min_length=1)
    shipped_window_hours: int = Field(gt=0)
    shipped_limit: int = Field(gt=0)
    waiting_limit: int = Field(gt=0)
    stoppage_limit: int = Field(gt=0)
    note_chars: int = Field(gt=20)
    hold_exemption_markers: tuple[str, ...] = Field(min_length=1)
    hold_exemption_slug_tokens: int = Field(gt=0)
    lane_live_window_minutes: int = Field(gt=0)
    stall_minutes: int = Field(gt=0)
    status_chars: int = Field(gt=20)
    title_chars: int = Field(gt=20)
    lane_limit: int = Field(gt=0)
    pool_stale_minutes: int = Field(gt=0)
    lane_kinds: tuple[ModelFloorLaneKindRule, ...] = Field(min_length=1)
    lane_kind_default: str = Field(min_length=1)
    codex_model_markers: tuple[str, ...] = Field(min_length=1)
    cloud_host_prefix: str = Field(min_length=1)
    cloud_no_push_statuses: tuple[str, ...] = Field(min_length=1)
    cloud_ready_logins: tuple[str, ...] = Field(min_length=1)
    cloud_no_push_lamp_text: str = Field(min_length=1)
    cloud_sessions_limit: int = Field(gt=0)
    repair_reason_chars: int = Field(gt=20)
    repair_reason_markers: tuple[str, ...] = Field(min_length=1)
    undrawn_not_found_cause: str = Field(min_length=1)
    undrawn_no_tasks_cause: str = Field(min_length=1)
    stop_outcomes: tuple[str, ...] = Field(min_length=1)
    ask_recipients: tuple[str, ...] = Field(min_length=1)
    ask_answer_kinds: tuple[str, ...] = Field(min_length=1)


class ModelFactoryFloorRequest(BaseModel):
    """Everything one run reads. The node has no clock: ``generated_at`` is a fact the caller supplies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    config: ModelFactoryFloorConfig
    selection: ModelFloorSelection = Field(default_factory=ModelFloorSelection)
    plans: tuple[ModelFloorPlanInput, ...]
    watcher: ModelFloorWatcher
    ledger: tuple[ModelFloorLedgerRow, ...] = ()
    linear: tuple[ModelFloorLinearIssue, ...] = ()
    placement: tuple[ModelHostCapacityAdvertisement, ...] = ()
    stoppages: tuple[ModelFloorStoppage, ...] = ()
    machines: tuple[ModelFloorMachineDecl, ...] = ()
    # A source that could not be read is None, never an empty tuple: the record then says "unread", not "none".
    pool: tuple[ModelFloorPoolReading, ...] | None = None
    host_table: tuple[ModelFloorHostRow, ...] | None = None
    placed: tuple[ModelFloorPlacedHost, ...] | None = None
    receipts: tuple[ModelFloorReceipt, ...] | None = None
    cloud_accounts: tuple[ModelFloorCloudAccount, ...] | None = None
    runner_fleet: ModelFloorRunnerFleet | None = None
    # Why runner_fleet is None, in the reader's own words; empty when it was read.
    runner_fleet_cause: str = ""
    # One verdict per host read by node_lane_container_budget_compute; None when no host was read.
    lane_containers: tuple[ModelLaneContainerBudgetVerdict, ...] | None = None

    _utc = field_validator("generated_at")(_aware)
