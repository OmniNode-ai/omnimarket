# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the floor's delegation tracker (OMN-23081).

The tracker answers two questions from records the factory already keeps: does every delegation scenario pass on its latest
scheduled run (the shippable line), and where is each ticket of the delegation readiness plan. A source that cannot be read
is ``unread`` and is never counted as a pass, a merge or a start condition that holds.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


ScenarioState = Literal["pass", "fail", "no_run", "no_workflow", "unread"]
ScenarioReadKind = Literal["read", "no_workflow", "unread"]
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
    """One delegation scenario: the scheduled workflow whose latest run is its result."""

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    repo: str = Field(
        min_length=1, description="owner/name of the repository that holds the workflow"
    )
    workflow: str = Field(min_length=1, description="the workflow file name")
    cadence_hours: int = Field(gt=0)
    builder_ticket: str | None = Field(
        default=None,
        description="the plan ticket that builds the workflow; none when the workflow already exists",
    )


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
    scenario_missing_refresh_seconds: int = Field(gt=0)
    scenario_reads_max_age_seconds: int = Field(gt=0)
    watcher_max_age_seconds: int = Field(gt=0)
    record_max_age_seconds: int = Field(gt=0)
    details_page_size: int = Field(gt=0)
    live_lane_dead_states: tuple[str, ...]


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


# ---- the tracker document ------------------------------------------------------------------------------------------------


class ModelScenarioResult(_Frozen):
    id: str
    title: str
    repo: str
    workflow: str
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


class ModelFloorDelegationRequest(_Frozen):
    """The flags of one run: which stage, and where the registry home is."""

    stage: Literal["scenarios", "build", "run"]
    home: str | None = None


class ModelFloorDelegationResult(_Frozen):
    ok: bool
    stage: Literal["scenarios", "build", "run"]
    wrote: str = ""
    detail: str = ""
    shippable: bool | None = None
