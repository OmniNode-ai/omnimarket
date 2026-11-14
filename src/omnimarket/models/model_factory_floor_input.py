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

from omnimarket.models.floor_17a.model_floor_17a import ModelGoalScope
from omnimarket.models.model_floor_line_select import (
    LineClass,
    ModelFloorLineGoal,
    ModelFloorLineProcess,
    ModelFloorLineSelectConfig,
)
from omnimarket.models.model_floor_phoenix_traces import ModelFloorPhoenixTraces
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_lane_container_budget import (
    ModelLaneContainerBudgetVerdict,
)
from omnimarket.models.model_merged_running_count import ModelMrcEvidence

ItemState = Literal["done", "integrating", "active", "prep", "wait", "stop"]
TableTaskState = Literal["merged", "open-green", "open-red", "not-started", "unread"]
PrState = Literal["OPEN", "MERGED", "CLOSED"]
CiState = Literal["green", "red", "pending", "none", "unread"]
StationGrouping = Literal["task", "wave"]
EvidenceCell = Literal["task", "ticket", "pr"]
ScopeStatus = Literal["valid", "needs_review"]


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
    # The state the manifest records for the task. Read only when the ledger, the watcher and Linear give the task no
    # evidence; a recorded "done" is never taken without a merged pull request.
    state: ItemState | None = None
    # True for a task read from a plan's own paragraphs: its id (T1, S3) is not unique across plans, so only its
    # tickets and pull requests link it to the ledger and the watcher, never the bare id.
    scoped: bool = False


class ModelFloorManifest(BaseModel):
    """The task manifest beside a plan."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str = ""
    epic: str = ""
    tasks: tuple[ModelFloorManifestTask, ...]
    # A plan whose task catalogue is under review says so here: the line then reads its progress as unknown and its
    # condition as needs_review until the catalogue is reconciled. No task is dropped.
    scope_status: ScopeStatus = "valid"
    scope_note: str = ""


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


class ModelFloorContext(BaseModel):
    """One check context of a pull request's head, with the result the watcher recorded for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    # The run's conclusion when it completed (``success``, ``failure``), else its status (``in_progress``, ``queued``).
    result: str = Field(min_length=1)


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
    # The head the CI read below belongs to; empty when the watcher recorded none (the head is then unread, not guessed).
    head_sha: str = ""
    contexts: tuple[ModelFloorContext, ...] = ()


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
    # The parent_ticket= cell a child CLAIM carries: the ticket of the work this row's ticket was minted under.
    parent_tickets: tuple[str, ...] = ()
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
    run_id: str = ""
    # The owns= cell as written: a comma list of what the row's lane says it owns (``floor:<holdup id>`` for the floor).
    owns: tuple[str, ...] = ()
    # The head= cell (a RELEASE or a hand-off names the head it covers), the ruling id a row cites, and the mechanism
    # ticket a RULING names.
    head: str = ""
    ruling: str = ""
    mechanism: str = ""
    # The closing row's typed needs= cell as written (``<action>:<target>`` or ``unparsed``) and its report= cell: what
    # the lane found and what it asks for next (OMN-23417).
    needs: str = ""
    report: str = ""

    _utc = field_validator("ts")(_aware)


class ModelFloorLinearIssue(BaseModel):
    """One issue of the Linear snapshot plans-today writes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    status: str = ""
    status_type: str = ""
    title: str = ""
    # The ids of the issue's children as the snapshot read them (empty when the snapshot did not read them): the floor
    # walks them to tell whether a lane's ticket sits under a root of the goal's ticket rule (OMN-22934).
    children: tuple[str, ...] = ()


class ModelFloorStoppage(BaseModel):
    """One friction class from the friction report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    occurrences: int = Field(ge=0)
    cost_minutes: float = Field(ge=0.0)
    recurring: bool = False
    tickets: tuple[str, ...] = ()
    last_seen: str = ""
    # When a STATUS row reported the class resolved (the friction report's ``resolved_at``); empty when none did.
    resolved_at: str = ""


class ModelFloorSelection(BaseModel):
    """Which plans are lines: a plan whose path contains any text; empty selects every plan given."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path_contains: tuple[str, ...] = ()
    # The plan files the newest readable plans-today output selected (class plan), in the output's order. None: no output was supplied, and
    # a run that declares no line draws every plan it is given, as before.
    plans_today_paths: tuple[str, ...] | None = None
    # The line classes this run draws. Declared and plan lines are the floor's own; the other four are drawn only when
    # the run names them, so a caller that does not forward their facts yet draws the record it always did.
    line_classes: tuple[LineClass, ...] = ("declared", "plan")
    # The declared line (a plan_path_contains text) that shows each lane's application containers; empty shows none.
    container_read_line: str = ""


class ModelFloorMachineDecl(BaseModel):
    """A machine the deployment overlay shows as a bay. A machine not declared here is never drawn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    label: str = Field(min_length=1)
    runner_labels: tuple[str, ...] = ()


class ModelFloorPoolDisk(BaseModel):
    """One disk cell of a pool reading: the volume's name and its free and total size in GB."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    free_gb: float = Field(ge=0.0)
    total_gb: float = Field(gt=0.0)


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
    lane_slots: int | None = None
    refusal: str = ""
    # Every ``disk_<name>=free/total/inode-pct`` cell, and the memory pressure cell (the Linux ``mempsi`` figure, or the
    # macOS ``pressure`` level) with the kind of that figure. Empty or None when the reading carried no such cell.
    disks: tuple[ModelFloorPoolDisk, ...] = ()
    mem_pressure: float | None = None
    mem_pressure_kind: Literal["", "psi", "level"] = ""

    _utc = field_validator("read_at")(_aware)


class ModelFloorHostRow(BaseModel):
    """One row of the shared lab host table: its cap and whether the host is out of selection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    cap: int | None = None
    available: bool = True
    unavailable_reason: str = ""
    # The machine the publisher runs on: out of placement, still heartbeats.
    local: bool = False


class ModelFloorDriftHost(BaseModel):
    """One host of the host-drift fleet projection: its newest heartbeat and how many drift items are open on it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    last_heartbeat_at: datetime | None = None
    open_items: int = Field(ge=0)

    _utc = field_validator("last_heartbeat_at")(
        lambda value: None if value is None else _aware(value)
    )


class ModelFloorHostDrift(BaseModel):
    """The host-drift fleet projection as the floor read it: when it was folded and what it holds per host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    folded_at: datetime
    hosts: tuple[ModelFloorDriftHost, ...]

    _utc = field_validator("folded_at")(_aware)


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
    # The Claude token pool label the lane started under, "" when the receipt names none.
    login: str = ""
    pr_heads: tuple[str, ...] = ()
    result: str = ""
    session_detail: str = ""
    session_needs: str = ""
    # The receipt's pid belongs to its launching host, where the floor reader runs.
    runner_alive: bool | None = None
    # When the runner's streamed output file (``.out`` beside the receipt) was last written; None when unread or absent.
    # A remote lane writes no ledger row between CLAIM and TERMINAL, so this is its progress evidence (OMN-23081).
    output_at: datetime | None = None

    _utc = field_validator("started_at")(_aware)

    @field_validator("output_at")
    @classmethod
    def _output_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)


class ModelFloorLaneProcess(BaseModel):
    """A controller worker's existing group/tag probe, read on the controller host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    started_at: datetime
    observed_at: datetime
    alive: bool | None
    detail: str

    _utc = field_validator("started_at", "observed_at")(_aware)


class ModelFloorCloudAccount(BaseModel):
    """One cloud account of the cloud pool file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    identity: Literal["machine", "account"] = "machine"
    login: str = ""
    cap: int | None = None
    active: tuple[str, ...] = ()
    credit_remaining: float | None = None
    credit_read_at: str = ""
    rate_status: str = ""
    rate_resets_at: str = ""
    # The rate headers the runner writes (OMN-22010): the two subscription windows, and when the record was read.
    rate_read_at: str = ""
    five_hour_utilization: float | None = None
    five_hour_resets_at: str = ""
    seven_day_utilization: float | None = None
    seven_day_resets_at: str = ""
    credit_source: str = ""


class ModelFloorRunner(BaseModel):
    """One CI runner of the runner fleet observation: the host label it carries and its status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_label: str = Field(min_length=1)
    status: Literal["online", "offline", "busy"]
    # Saved by a producer that reports them; an older saved file has none, and such a row cannot be told apart.
    runner_name: str | None = Field(default=None, min_length=1)
    label_class: str | None = Field(default=None, min_length=1)
    labels: tuple[str, ...] = ()


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


class ModelFloorLimitsConfig(BaseModel):
    """What the Limits panel judges against, from the contract: never a literal in the handler (OMN-22010)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # A reading older than this many minutes (one publish cycle) is UNKNOWN, never a number.
    publish_cycle_minutes: int = Field(gt=0)
    # The month's budget for an API key whose overlay row names none.
    api_budget_usd: float = Field(gt=0)
    # The day the cloud-session credit expires (YYYY-MM-DD).
    cloud_credit_expiry: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class ModelLimitApiKey(BaseModel):
    """One Anthropic API key as the meter read it: spend so far this calendar month (UTC)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    month_spend_usd: float | None = None
    read_at: str = ""
    source: str = Field(min_length=1)
    # A budget the deployment overlay sets for this key; else the contract's api_budget_usd.
    budget_usd: float | None = None


class ModelLimitEngine(BaseModel):
    """A usage window of a subscription engine (Codex on ChatGPT, the GLM coding plan)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    used_pct: float | None = None
    window: str = ""
    resets_at: str = ""
    read_at: str = ""
    source: str = Field(min_length=1)


class ModelLimitLabRung(BaseModel):
    """One local lab model rung: whether it answers and when it last passed a smoke."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    available: bool | None = None
    last_smoke_at: str = ""
    last_smoke_ok: bool | None = None
    read_at: str = ""
    source: str = Field(min_length=1)


class ModelLimitToken(BaseModel):
    """One token of a host's Claude token pool: a label and its state, never a value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    limited_until: str = ""
    revoked: bool = False


class ModelLimitTokenPool(BaseModel):
    """One lab host's Claude token pool (the pool's status mirror)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    read_at: str = ""
    source: str = Field(min_length=1)
    tokens: tuple[ModelLimitToken, ...] = ()


class ModelFloorLimitsFacts(BaseModel):
    """Every limit the caller read. A source that could not be read is None, never empty: the row then says UNKNOWN."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_keys: tuple[ModelLimitApiKey, ...] | None = None
    codex: ModelLimitEngine | None = None
    glm: ModelLimitEngine | None = None
    lab_rungs: tuple[ModelLimitLabRung, ...] | None = None
    token_pools: tuple[ModelLimitTokenPool, ...] | None = None
    # The day's idle slot-hours, summed by whoever samples the slots; None when nothing sampled them.
    idle_slot_hours: float | None = None
    idle_slot_hours_read_at: str = ""


class ModelFloorHostStatusConfig(BaseModel):
    """The bounds and names the per-host Machines record reads; the handler holds none of them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # A host-drift heartbeat older than this, or a fleet projection folded longer ago, is stale.
    heartbeat_stale_seconds: int = Field(gt=0)
    # The pool reading's disk cell that decides lane admission (``runs`` is the run directories' volume).
    disk_cell: str = Field(min_length=1)
    # A capacity advertisement older than this is stale (the file is a snapshot taken once per publish cycle).
    advert_stale_minutes: int = Field(gt=0)
    # The advertised tool name that says the host runs containers.
    docker_tool: str = Field(min_length=1)
    # The lane kind (floor.lanes[].kind) of a landing worker.
    landing_worker_kind: str = Field(min_length=1)


class ModelGoalTicketRoots(BaseModel):
    """The ``goal_scope_ticket_rule`` overlay as the floor reads it: the tickets whose work the goal keeps.

    The landing tick owns the overlay, so the keys the floor does not read are ignored here and checked there.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    ticket_roots: tuple[str, ...] = ()


class ModelFloorStoppedConfig(BaseModel):
    """What makes a lane that ended short a stopped line on the floor (OMN-22934); every value is the contract's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The TERMINAL outcomes that leave work behind; a lane whose newest TERMINAL has another outcome is never listed.
    outcomes: tuple[str, ...] = Field(min_length=1)
    # A stop older than this many minutes is no longer listed.
    max_age_minutes: int = Field(gt=0)
    # Tickets that many lanes share: they never tie two lanes to the same work.
    umbrella_tickets: tuple[str, ...] = ()
    # Regular expressions (first group, or the whole match) that read a branch name out of a row's text.
    branch_patterns: tuple[str, ...] = Field(min_length=1)
    # A regular expression that reads a commit head out of a row's text, after the word head.
    head_pattern: str = Field(min_length=1)
    # Words in the ending row that say the lane asked for something or left a next step (matched case-insensitively).
    ask_markers: tuple[str, ...] = Field(min_length=1)
    next_step_markers: tuple[str, ...] = Field(min_length=1)
    # A lane name with this regular expression removed is its stem: a later lane of the same stem is its successor.
    lane_stem_suffix: str = Field(min_length=1)
    # Parent steps walked from a lane's ticket up to a root of the goal's ticket rule through the Linear snapshot's child
    # ids (the rule's own bound is max_depth in the goal_scope_ticket_rule overlay).
    scope_max_depth: int = Field(ge=1)
    # How many of the lane's last rows the record carries, and each row cut at this many characters.
    rows_kept: int = Field(ge=1)
    row_chars: int = Field(ge=40)
    # Where the lane's own report is, relative to the registry home; {lane} is the lane name.
    report_path_template: str = Field(min_length=1)


class ModelFactoryFloorConfig(BaseModel):
    """The vocabularies and limits the contract's ``config`` block declares; the handler holds none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str = Field(min_length=1)
    andon_markers: tuple[str, ...] = Field(min_length=1)
    gate_kind: str = Field(min_length=1)
    gate_state: str = Field(min_length=1)
    # Alternate wave subjects emitted by ledger gate runners; empty preserves older configuration.
    gate_wave_subject_template: str = ""
    gate_fail_verdicts: tuple[str, ...] = Field(min_length=1)
    gate_pass_verdicts: tuple[str, ...] = Field(min_length=1)
    done_outcomes: tuple[str, ...] = Field(min_length=1)
    # A lane that ended by handing its pull request to landing has built nothing that runs until the request merges:
    # the task is active while a pull request it named is unmerged, and done once all of them have merged.
    handoff_outcomes: tuple[str, ...] = ()
    # Done means merged, running on dev and integration-proven. The proof is the newest STATUS row of the item (named by
    # its pull request, its ticket or its task id) in this state, written after the item's newest merge: a verdict in
    # the pass list proves it, one in the fail list names a failed proof, anything else is no proof yet.
    integration_state: str = Field(min_length=1)
    integration_pass_verdicts: tuple[str, ...] = Field(min_length=1)
    integration_fail_verdicts: tuple[str, ...] = Field(min_length=1)
    # Decimal places of the progress percent (rounded half-up).
    percent_decimals: int = Field(ge=0, le=3)
    # The progress sentences. {completed} {total} fill the known one; {note} the plan's own scope note.
    progress_note_known: str = Field(min_length=1)
    progress_note_no_tasks: str = Field(min_length=1)
    progress_note_unscoped: str = Field(min_length=1)
    progress_note_duplicate_ids: str = Field(min_length=1)
    progress_note_under_review: str = Field(min_length=1)
    progress_note_running_unread: str = Field(min_length=1)
    # The blocker of a line whose work tasks are all done while a gate has not passed; {gates} are the waiting ids.
    pending_gate_blocker: str = Field(min_length=1)
    blocked_outcomes: tuple[str, ...] = Field(min_length=1)
    live_claim_minutes: int = Field(gt=0)
    station_grouping: StationGrouping
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
    # The typed cells of a ledger row that bind it to a task as evidence; a task id in a row's free text binds nothing.
    evidence_cells: tuple[EvidenceCell, ...] = Field(min_length=1)
    # The state= value of the STATUS row that maps a task (task=) of a plan (subject=) to its ticket= and pr=.
    task_map_state: str = Field(min_length=1)
    # The note a built task carries while a gate it depends on reads a fail verdict; such a task is not counted built.
    built_ahead_of_gate_label: str = Field(min_length=1)
    limits: ModelFloorLimitsConfig
    host_status: ModelFloorHostStatusConfig
    stopped: ModelFloorStoppedConfig
    line_select: ModelFloorLineSelectConfig


class ModelFloorSourceReads(BaseModel):
    """When the caller read each source, so the record can say how old the oldest source a line depends on is.

    A source with no read time is None: its age is then not guessed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger: datetime | None = None
    linear: datetime | None = None
    running_evidence: datetime | None = None

    _utc = field_validator("ledger", "linear", "running_evidence")(
        lambda value: None if value is None else _aware(value)
    )


SourceHealthState = Literal["read", "read-empty", "stale", "unread"]


class ModelFloorSourceHealth(BaseModel):
    """How one source was read: ``read``, ``read-empty`` (read, holds nothing), ``stale`` (older than its bar) or ``unread``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    state: SourceHealthState
    read_at: datetime | None = None
    cause: str = ""

    _utc = field_validator("read_at")(
        lambda value: None if value is None else _aware(value)
    )


class ModelFloorRequiredContexts(BaseModel):
    """The contexts a repository's landing branch requires, from the queue-status tick's saved map."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    contexts: tuple[str, ...] = ()
    read_at: datetime | None = None

    _utc = field_validator("read_at")(
        lambda value: None if value is None else _aware(value)
    )


class ModelFloorGoalRow(BaseModel):
    """One row of the goal file: ``id | what | verdict | next action | probe``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(min_length=1)
    what: str = ""
    verdict: str = ""
    next_action: str = ""
    probe: str = ""


class ModelFloorProcessDecl(BaseModel):
    """A factory process the deployment overlay declares, with the run record it writes and what that record covers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    # The ledger row kind and cell match that is one run of the process, and the match that says that run did nothing.
    run_kind: str = Field(min_length=1)
    run_match: str = Field(min_length=1)
    did_nothing_match: str = ""
    interval_min: int = Field(gt=0)
    # The outcomes (``success``, ``did-nothing``, ``error``) the process's own contract says its run record covers. A
    # process that does not cover all three is a process whose silence is not detectable.
    outcomes_covered: tuple[str, ...] = ()


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
    # Records that a merge runs on dev (a deploy receipt, an adoption row), each naming the merged pull request. None
    # when no reader could read them: an unread source never shows a merge as running.
    running_evidence: tuple[ModelMrcEvidence, ...] | None = None
    source_reads: ModelFloorSourceReads = Field(default_factory=ModelFloorSourceReads)
    placement: tuple[ModelHostCapacityAdvertisement, ...] = ()
    stoppages: tuple[ModelFloorStoppage, ...] = ()
    machines: tuple[ModelFloorMachineDecl, ...] = ()
    # A source that could not be read is None, never an empty tuple: the record then says "unread", not "none".
    pool: tuple[ModelFloorPoolReading, ...] | None = None
    host_table: tuple[ModelFloorHostRow, ...] | None = None
    placed: tuple[ModelFloorPlacedHost, ...] | None = None
    receipts: tuple[ModelFloorReceipt, ...] | None = None
    lane_processes: tuple[ModelFloorLaneProcess, ...] | None = None
    cloud_accounts: tuple[ModelFloorCloudAccount, ...] | None = None
    cloud_accounts_cause: str = ""
    runner_fleet: ModelFloorRunnerFleet | None = None
    # Why runner_fleet is None, in the reader's own words; empty when it was read.
    runner_fleet_cause: str = ""
    # One verdict per host read by node_lane_container_budget_compute; None when no host was read.
    lane_containers: tuple[ModelLaneContainerBudgetVerdict, ...] | None = None
    # One Phoenix trace reading; None when the Phoenix URL is not configured (the floor draws it unread).
    phoenix_traces: ModelFloorPhoenixTraces | None = None
    # Every limit read for the Limits panel; a field left None is an unread source (OMN-22010).
    limits: ModelFloorLimitsFacts = Field(default_factory=ModelFloorLimitsFacts)
    # The host-drift fleet projection; None when it could not be read, with the reader's words in host_drift_cause.
    host_drift: ModelFloorHostDrift | None = None
    host_drift_cause: str = ""
    # The landing goal's scope; None when it could not be read, and then no stopped lane is listed (an unread scope is
    # never everything in scope).
    goal_scope: ModelGoalScope | None = None
    # The ticket roots of the goal's ticket rule (read with goal_scope): work on a root ticket is in scope. A child of a
    # root is in scope when the Linear snapshot's child ids (``linear``) lead from the root down to it, within the stopped
    # config's scope_max_depth steps; with no snapshot read only the roots and goal_scope's own tickets are in scope.
    scope_ticket_roots: tuple[str, ...] = ()
    # Blocked, partial and handed-off TERMINAL rows and handed-off STATUS rows of the live ledger and every archive roll,
    # with no age limit, that are older than the ledger window (the window's own rows are in ``ledger``). None when the
    # ledger reader could not read them.
    ledger_unresolved: tuple[ModelFloorLedgerRow, ...] | None = None
    # The required contexts per repository and the open goal rows; None when the source could not be read.
    required_contexts: tuple[ModelFloorRequiredContexts, ...] | None = None
    goal_rows: tuple[ModelFloorGoalRow, ...] | None = None
    # The deployment overlay's line board declarations.
    processes: tuple[ModelFloorProcessDecl, ...] = ()
    roles: tuple[str, ...] = ()
    release_required_repos: tuple[str, ...] = ()
    held_gates: tuple[str, ...] = ()
    # How each source the board depends on was read.
    # The goal rows and declared processes the line selection (classes goal and process) draws its lines from.
    line_goals: tuple[ModelFloorLineGoal, ...] = ()
    line_processes: tuple[ModelFloorLineProcess, ...] = ()
    source_health: tuple[ModelFloorSourceHealth, ...] = ()

    _utc = field_validator("generated_at")(_aware)
