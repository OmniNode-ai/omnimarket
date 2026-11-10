# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the floor's delegation tracker (OMN-23081).

The tracker answers two questions from records the factory already keeps: does every delegation scenario pass on its latest
scheduled run (the shippable line), and where is each ticket of the delegation readiness plan. A source that cannot be read
is ``unread`` and is never counted as a pass, a merge or a start condition that holds.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_floor_delegation_effect.models.model_delegation_routes import (
    ModelRoutesBlock,
    ModelRoutesSource,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


ScenarioState = Literal["pass", "fail", "no_run", "no_workflow", "unread"]
ScenarioReadKind = Literal["read", "no_workflow", "unread"]
ScenarioSource = Literal["workflow", "lab_node"]
TicketGroup = Literal[
    "done",
    "merged_awaiting_closer",
    "pr_open",
    "lane_running",
    "not_started",
    "unread",
]
PrCi = Literal["red", "green", "pending", "unread"]
PrState = Literal["open", "merged", "closed"]


# ---- the contract's runtime block ------------------------------------------------------------------------------------


class ModelScenarioDecl(_Frozen):
    """One delegation scenario and the source of its latest scheduled result, chosen per scenario by the overlay.

    ``workflow``: the latest scheduled run of a hosted workflow (``repo`` and ``workflow`` name it). ``lab_node``: the latest
    scheduled result a lab node wrote as a STATUS row under ``lane``, read from the bus.
    """

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source: ScenarioSource = "workflow"
    repo: str = Field(
        default="",
        description="owner/name of the repository that holds the workflow (source workflow)",
    )
    workflow: str = Field(
        default="", description="the workflow file name (source workflow)"
    )
    lane: str = Field(
        default="",
        description="the ledger lane the node writes its results under (source lab_node)",
    )
    cadence_hours: int = Field(gt=0)
    builder_ticket: str | None = Field(
        default=None,
        description="the plan ticket that builds the scenario; none when it already exists",
    )

    @model_validator(mode="after")
    def _names_its_source(self) -> ModelScenarioDecl:
        if self.source == "workflow" and not (self.repo and self.workflow):
            raise ValueError(
                f"scenario {self.id}: a workflow source names a repo and a workflow"
            )
        if self.source == "lab_node" and not self.lane:
            raise ValueError(f"scenario {self.id}: a lab_node source names its lane")
        return self


class ModelOperatorAnswer(_Frozen):
    """An open question of the plan that the operator has answered, with the ledger ruling that records it."""

    id: str = Field(pattern=r"^Q[0-9]+$")
    ruling: str = Field(min_length=1)


class ModelDelegationContractRuntime(_Frozen):
    """The ``runtime`` block of contract.yaml: where the sources are and how long each may take."""

    home_env: str = Field(min_length=1)
    onex_relpath: str = Field(min_length=1)
    overlay_file_env: str = Field(min_length=1)
    overlay_id: str = Field(min_length=1)
    record_relpath: str = Field(min_length=1)
    watcher_relpath: str = Field(min_length=1)
    ticket_mirror_relpath: str = Field(min_length=1)
    scenario_reads_relpath: str = Field(min_length=1)
    output_relpath: str = Field(min_length=1)
    scenario_event: str = Field(min_length=1)
    scenario_run_limit: int = Field(gt=0)
    scenario_read_timeout_seconds: int = Field(gt=0)
    scenario_stage_deadline_seconds: int = Field(gt=0)
    scenario_refresh_seconds: int = Field(gt=0)
    scenario_reads_max_age_seconds: int = Field(gt=0)
    ledger_read_command_env: str = Field(min_length=1)
    ledger_read_timeout_seconds: int = Field(gt=0)
    lab_node_window_days: int = Field(ge=1, le=31)
    lab_node_rows_page_size: int = Field(gt=0)
    lab_node_max_pages: int = Field(gt=0)
    watcher_max_age_seconds: int = Field(gt=0)
    record_max_age_seconds: int = Field(gt=0)
    details_page_size: int = Field(gt=0)
    live_lane_dead_states: tuple[str, ...]
    # The routes reading (OMN-23381): the file the routes stage writes, how old it may be before it is drawn stale, and the table.
    route_reads_relpath: str = Field(min_length=1)
    routes_max_age_min: int = Field(gt=0)
    routes_read_timeout_seconds: int = Field(gt=0)
    routes_table: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    routes_data_source: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")


class ModelDelegationOverlay(_Frozen):
    """The deployment overlay: which repositories, workflows and tickets the tracker follows.

    These are deployment facts of a private estate, so they live in the overlays home and never in this package.
    """

    github_url: str = Field(min_length=1)
    github_org: str = Field(min_length=1)
    ticket_url_prefix: str = Field(min_length=1)
    plan_ticket_prefix: str = Field(min_length=1)
    plan_ticket_first: int = Field(gt=0)
    plan_ticket_last: int = Field(gt=0)
    scenarios: tuple[ModelScenarioDecl, ...] = Field(min_length=1)
    operator_answers: tuple[ModelOperatorAnswer, ...] = ()
    # Where the delegation events are read for the routes block. Without it the block is unread, with the reason named.
    routes: ModelRoutesSource | None = None


class ModelDelegationRuntime(ModelDelegationContractRuntime, ModelDelegationOverlay):
    """The contract's runtime block and the overlay together, which is what the readers and the build take."""


# ---- what the readers hand to the pure build ---------------------------------------------------------------------------


class ModelScenarioRun(_Frozen):
    run_id: int
    status: str
    conclusion: str
    event: str
    created_at: str
    updated_at: str
    url: str


class ModelScenarioRead(_Frozen):
    """One scenario's workflow runs as the source node answered, newest first."""

    scenario: str
    read: ScenarioReadKind
    read_at: str
    runs: tuple[ModelScenarioRun, ...] = ()
    reason: str = ""


class ModelScenarioReads(_Frozen):
    """The file the scenario read step writes and the build step reads."""

    updated_at: str
    reads: tuple[ModelScenarioRead, ...]


class ModelMirrorTicket(_Frozen):
    id: str
    title: str
    plan_task: str = ""
    linear_status: str
    status_type: str
    start_when: str = ""
    read_at: str = ""


class ModelWatcherPr(_Frozen):
    ref: str
    repo: str
    number: int
    ticket: str
    title: str
    state: PrState
    ci: PrCi
    ci_note: str = ""
    ci_read_at: str = ""
    url: str


class ModelDelegationLane(_Frozen):
    lane: str
    ticket: str
    host: str
    liveness: str
    stalled: bool = False
    started_at: str = ""


class ModelSourceReading(_Frozen):
    """When one input was read and whether it was fresh enough to trust."""

    id: str
    title: str
    ok: bool
    read_at: str = ""
    age_seconds: int | None = None
    detail: str = ""


class ModelDelegationInputs(_Frozen):
    scenario_reads: ModelScenarioReads | None
    mirror: tuple[ModelMirrorTicket, ...]
    prs: tuple[ModelWatcherPr, ...]
    lanes: tuple[ModelDelegationLane, ...]
    sources: tuple[ModelSourceReading, ...]
    routes: ModelRoutesBlock | None = None


# ---- the tracker document ------------------------------------------------------------------------------------------------


class ModelScenarioResult(_Frozen):
    id: str
    title: str
    source: ScenarioSource = "workflow"
    repo: str
    workflow: str
    lane: str = ""
    cadence_hours: int
    state: ScenarioState
    result_text: str
    latest_at: str = ""
    latest_url: str = ""
    latest_conclusion: str = ""
    age_seconds: int | None = None
    overdue: bool = False
    in_progress: bool = False
    recent: tuple[str, ...] = ()
    reason: str = ""
    builder_ticket: str | None = None
    builder_group: TicketGroup | None = None
    workflow_url: str = ""


class ModelStartPart(_Frozen):
    text: str
    met: bool | None


class ModelTicketPr(_Frozen):
    ref: str
    title: str
    state: PrState
    ci: PrCi
    ci_note: str
    url: str


class ModelTicketResult(_Frozen):
    id: str
    title: str
    plan_task: str
    group: TicketGroup
    linear_status: str
    mirror_read_at: str
    url: str
    prs: tuple[ModelTicketPr, ...] = ()
    pr_ci: PrCi | None = None
    lane: str = ""
    host: str = ""
    start_condition: bool | None = None
    start_text: str = ""
    start_parts: tuple[ModelStartPart, ...] = ()
    start_note: str = ""
    why: str


class ModelGroupCount(_Frozen):
    id: str
    title: str
    count: int
    ticket_ids: tuple[str, ...]


class ModelDelegationCounts(_Frozen):
    scenarios_total: int
    scenarios_passing: int
    scenarios_failing: int
    scenarios_not_built: int
    scenarios_no_run: int
    scenarios_unread: int
    tickets_total: int
    groups: tuple[ModelGroupCount, ...]
    pr_open_red: int
    pr_open_green: int
    pr_open_pending: int
    pr_open_unread: int
    start_true: int
    start_false: int
    start_unread: int


class ModelDelegationTracker(_Frozen):
    """The served document: delegation-tracker.json."""

    schema_id: Literal["omnimarket.floor_delegation.v1"] = (
        "omnimarket.floor_delegation.v1"
    )
    generated_at: str
    shippable: bool
    shippable_line: str
    shippable_rule: str
    blocking: tuple[str, ...]
    counts: ModelDelegationCounts
    scenarios: tuple[ModelScenarioResult, ...]
    tickets: tuple[ModelTicketResult, ...]
    sources: tuple[ModelSourceReading, ...]
    details_page_size: int
    routes: ModelRoutesBlock


class ModelFloorDelegationRequest(_Frozen):
    """The flags of one run: which stage, and where the registry home is."""

    stage: Literal["scenarios", "routes", "build", "run"]
    home: str | None = None


class ModelFloorDelegationResult(_Frozen):
    ok: bool
    stage: Literal["scenarios", "routes", "build", "run"]
    wrote: str = ""
    detail: str = ""
    shippable: bool | None = None
