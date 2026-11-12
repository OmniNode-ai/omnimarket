# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared models of the 17a floor (OMN-19998): emission, receipts, lab state, the simple floor and the alarm.

Everything a deployment decides (the pieces, the joins, the lab sources and their limits, the alarm channels) is an
overlay document validated by the ``Overlay`` models here; no value of that kind is in code.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.floor_17a.model_delegation_idle import (
    ModelDelegationIdleOverlay,
    ModelDelegationTicketIn,
)
from omnimarket.models.floor_17a.model_floor_integration import (
    ModelFloorIntegrationIn,
    ModelFloorIntegrationOverlay,
    ModelFloorIntegrationSection,
)
from omnimarket.models.floor_17a.model_mvp_launch import ModelMvpLaunchBlock
from omnimarket.models.floor_17a.model_worklist import (
    ModelWorklistClass,
    ModelWorklistItemIn,
    ModelWorklistOverlay,
)

ReadingState = Literal["WORKING", "NOT WORKING", "UNKNOWN"]
Phase = Literal["start", "success", "failure"]
# What kind of unit of work a chain came from (OMN-23862). ``real`` is a unit of factory work; ``probe`` and the rest are
# the factory's own test traffic. An emission that sets none is ``unknown``, and unknown never counts as real: which origins
# count is the readiness overlay's ``real_origins``, not code.
ChainOrigin = Literal["real", "probe", "twin", "control", "canary", "drill", "unknown"]
Draw = Literal["green", "amber", "red", "grey"]
ReadingVerdict = Literal["fresh", "stale", "failing", "unread"]
SourceKind = Literal[
    "hosts",
    "runner_groups",
    "disk",
    "queue_age",
    "deploy_lag",
    "broker_lag",
    "overlay_store",
    "deploy_agent",
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
    # The kind of work the chain came from (OMN-23862); unset is ``unknown``, never ``real``.
    origin: ChainOrigin = "unknown"
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
    # The join's judging bound (OMN-23086), declared by the overlay: a from-piece chain whose newest event is older than this
    # instant is not judged, because the join's to-piece could not have answered it. A chain at or after the bound is judged
    # as ever, so a chain that is really broken stays red. ``None`` judges every chain in the window.
    judged_since: AwareDatetime | None = None
    # Whether a to-piece typed failure under a from-piece success is a finding of this join (OMN-23086), declared by the overlay.
    # Off, such an answer is not read (the join reads not run or error only); on, each is named with the failure's code and
    # the join reads broken. A join with a running caller of its own turns it on, so a failed answer is told apart from a
    # missed run.
    names_failed_answers: bool = False


class ModelPieceDef(_Frozen):
    """One of the 18 pieces, as the overlay declares it."""

    piece: int = Field(ge=1, le=18)
    name: str = Field(min_length=1)
    reading_max_age_s: int = Field(gt=0)
    first_reading: str = ""
    # One plain sentence of what the piece is, for the main page's side panel; empty when the overlay declares none.
    what: str = ""


class ModelPieceChains(_Frozen):
    """The chains of one piece that a join judges, as the node that runs the piece declares them.

    A node can emit chains that belong to its piece but are not the pipeline a join reads: the floor cycle node also
    emits one chain per lab source row it reads. A row that reads a bad value ends in an error chain, which gives its
    piece an error receipt, and no downstream piece can answer it with a typed upstream refusal. A join therefore reads
    only the chains of ``nodes``; every other chain of the piece still counts toward the piece's own receipt.
    """

    piece: int = Field(ge=1, le=18)
    nodes: tuple[str, ...] = Field(min_length=1)


class ModelJoinEnd(_Frozen):
    """The node at which a join ends, when no node downstream of its piece can emit a typed outcome for it.

    A join normally ends at its ``to_piece``: when the from piece fails, the to piece must answer with a typed refusal under
    the same correlation id. Where no node of the to piece can give that refusal, the join is respecified to end at the last
    node that does emit a typed outcome: ``node``, a node of the from piece. The join is then judged on that node's chain
    alone: it must end in a success or a typed failure within the chain deadline. A typed failure is the end of the join, not
    a break, and no answer from the to piece is demanded.
    """

    join: str = Field(pattern=r"^J(\d{1,2}|-cycle)$")
    node: str = Field(min_length=1)


class ModelFloorReceiptInput(_Frozen):
    now: datetime
    emissions: tuple[ModelNodeEmission, ...]
    pieces: tuple[ModelPieceDef, ...]
    joins: tuple[ModelJoinDef, ...]
    freshness_max_age_s: int = Field(gt=0)
    error_max_age_s: int = Field(gt=0)
    chain_deadline_s: int = Field(gt=0)
    # The pieces a join may end at and still be judged: admitted pieces and pieces that have emitted. A join into a piece
    # that does not exist yet cannot be broken, so it is not judged. ``None`` judges every join.
    judged_to_pieces: tuple[int, ...] | None = None
    # For each piece listed, the nodes whose chains a join judges. A piece not listed is judged on every chain it emitted.
    declared_chains: tuple[ModelPieceChains, ...] = ()
    # For each join listed, the node of its from piece at which it ends. A join not listed ends at its to piece.
    join_ends: tuple[ModelJoinEnd, ...] = ()
    # The chain origins that are real work (OMN-23862), the readiness overlay's ``real_origins``. A join's ``real_golden_at``
    # reads only chains whose every event carries one of these; with none declared no chain is real.
    real_origins: tuple[ChainOrigin, ...] = ()


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
    "downstream_failed_after_upstream_succeeded",
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
    # The origin every event of the chain carries; ``unknown`` when the events disagree or set none (OMN-23862).
    origin: ChainOrigin = "unknown"
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
    # The newest golden chain of this join whose chains on both sides came from real work (OMN-23862); ``None`` when every
    # golden chain was probe traffic or of unknown origin. ``golden_at`` stays the health reading, probes included.
    real_golden_at: datetime | None = None


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


class ModelRunnerPlanRule(_Frozen):
    """What the host table and the plan say about one family of CI runners, named by a prefix of the runner name.

    The longest matching prefix decides. ``host`` names the machine of the floor record the family runs on; when that
    machine is declared out or in repair, the family's offline runners are parked on purpose and read as one line for
    the host. ``meant_to_run`` false is a family the plan leaves stopped. A family that is meant to run and is offline
    on a machine that is not declared out is a real problem and stays red, with ``restore`` saying what puts it back.
    """

    name_prefix: str = Field(min_length=1)
    host: str = Field(min_length=1)
    meant_to_run: bool
    plan: str = Field(min_length=1)
    restore: str = ""


class ModelOwnerSourceOverlay(_Frozen):
    """Where owner entries may come from (OMN-23388): the floor overlay file must equal its own git checkout's ``remote_ref``."""

    # The ref of the overlays repository's main (for example ``origin/main``); the floor overlay file, which carries the
    # owner map, must be byte-equal to the same file at this ref, or the cycle refuses it naming the source.
    remote_ref: str = Field(min_length=1)
    # How long one git read may take before the source counts as unreadable.
    git_timeout_s: int = Field(default=20, gt=0)


class ModelLabStateOverlay(_Frozen):
    """The overlay of the lab-state reader and node: the sources and how each is read."""

    sources: tuple[ModelLabSourceDef, ...] = Field(min_length=6)
    disk_keys: tuple[str, ...] = Field(min_length=1)
    broker_conditions: tuple[str, ...] = Field(min_length=1)
    queue_age_classes: tuple[str, ...] = Field(min_length=1)
    deploy_lag_lookback_days: int = Field(gt=0)
    require_runner_version: bool
    declared_host_states: tuple[str, ...] = ()
    # Hosts the pool probe does not read; every other host of the floor record needs a disk line in the pool receipt.
    disk_unprobed_hosts: tuple[str, ...] = ()
    # A host the pool refuses only on load that its own placed units explain is busy with our work, not failing (OMN-22870).
    # ``own_load_marker`` is the refusal part the placement prints when the units placed on the host explain the load over the
    # bar; ``load_bar_markers`` are the load bar's own refusal parts. A refusal made of those parts alone, with the own-load
    # part among them, reads fine; any other part (memory, disk, parity, heat, link) and a load nothing placed explains stay red.
    # An empty ``own_load_marker`` turns the reading off, so every refusal reads red as before.
    own_load_marker: str = ""
    load_bar_markers: tuple[str, ...] = ()
    refusal_separator: str = "; "
    # Merge states of the watcher that mean the branch conflicts: a green pull request in one is not waiting to be armed.
    conflict_merge_states: tuple[str, ...] = ()
    # The host table and plan rules that split the offline runners into parked on purpose and real problems.
    runner_plan: tuple[ModelRunnerPlanRule, ...] = ()
    # The deploy agent's address, how many of its results the floor keeps, and how long a read may wait.
    deploy_agent_url: str = ""
    deploy_agent_history: int = Field(default=50, gt=0)
    deploy_agent_timeout_s: int = Field(default=10, gt=0)
    # When declared, the floor overlay's owner entries are accepted only from this ref of the overlays repository
    # (OMN-23388); ``None`` checks no source.
    owner_source: ModelOwnerSourceOverlay | None = None

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


# --- the landing goal's scope ------------------------------------------------------------------------------------------

CLOSES_TICKET = re.compile(r"\bCloses\s+(OMN-\d+)\b", re.IGNORECASE)
ANY_TICKET = re.compile(r"\bOMN-\d+\b", re.IGNORECASE)
GoalScopeClass = Literal["in_scope", "paused", "unlisted"]


class ModelGoalScope(BaseModel):
    """The ``goal_scope`` overlay: which pull requests the operator's landing goal counts.

    The overlay is owned by the landing tick, so keys this floor does not read are ignored here and checked there. A
    pull request is in scope when it is listed (as in scope or as a landing prerequisite), when its repository is in
    scope whole, or when it names a kept ticket in its title, its head branch or a ``Closes OMN-n`` line of its body.
    One that is only in the paused list is paused; one nobody named is outside the goal as well.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    ruling: str = Field(min_length=1)
    in_scope_repos: tuple[str, ...] = ()
    in_scope_tickets: tuple[str, ...] = ()
    in_scope_prs: tuple[str, ...] = ()
    prerequisite_prs: tuple[str, ...] = ()
    paused_prs: tuple[str, ...] = ()

    def classify(
        self, ref: str, title: str = "", head_ref: str = "", body: str = ""
    ) -> GoalScopeClass:
        repo = ref.partition("#")[0]
        if (
            ref in self.in_scope_prs
            or ref in self.prerequisite_prs
            or repo in self.in_scope_repos
            or "*" in self.in_scope_repos
        ):
            return "in_scope"
        named = {
            t.upper()
            for t in (
                *ANY_TICKET.findall(f"{title} {head_ref}"),
                *CLOSES_TICKET.findall(body),
            )
        }
        if named & set(self.in_scope_tickets):
            return "in_scope"
        return "paused" if ref in self.paused_prs else "unlisted"


class ModelOutsideGoal(_Frozen):
    """What the landing goal does not count, so the top numbers can be read against the whole watcher."""

    read: bool
    open_prs: int | None = None
    merges_in_window: int | None = None
    paused_open: int | None = None
    ruling: str = ""
    text: str = ""


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
    # The pull request the lane works on ("repo#number"), the key that ties a lane to a pull request class.
    pr: str = ""
    # The process reading of the lane ("alive", "unread", "dead", "finished"): a dead or finished lane is on nothing.
    liveness: str = "unread"
    # The holdup ids the lane owns by an owns=floor:<id> cell on its CLAIM or a STATUS row.
    owns: tuple[str, ...] = ()
    # The ticket the lane's own ticket was minted under (the CLAIM's parent_ticket= cell): a lane on a child ticket is
    # on the work of a cause whose declared owner is that parent.
    parent_ticket: str = ""


class ModelFloorCauseIn(_Frozen):
    """A candidate cause, before the rule that decides its colour."""

    id: str
    title: str
    detail: str
    size: int = Field(ge=0)
    size_unit: str
    # Where the problem was seen: shown, never used to say who is working on it.
    tickets: tuple[str, ...] = ()
    # Who works on it: tickets the overlay or a lab source declares as owning this cause.
    owners: tuple[str, ...] = ()
    # The pull requests of a pull request class ("repo#number"): a lane on one of them is on the cause.
    prs: tuple[str, ...] = ()
    needs_operator: bool = False
    last_seen: datetime | None = None
    # When a STATUS row reported the stoppage resolved: a stoppage not seen since is not occurring, whatever the window.
    resolved_at: datetime | None = None
    # The cost the reporter logged for a stoppage (the reporter's own figure, not a measurement).
    reported_cost_minutes: float | None = None


class ModelFloorCause(_Frozen):
    id: str
    title: str
    detail: str
    size: int
    size_unit: str
    # ``quiet``: a stoppage whose own record says it is not occurring and nobody is needed on it. Listed, never muted.
    state: Literal["red", "amber", "quiet"]
    work_under_way: bool
    needs_operator: bool
    lanes: tuple[str, ...] = ()
    tickets: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    prs: tuple[str, ...] = ()
    listed: bool = True
    members: tuple[str, ...] = ()
    last_seen: datetime | None = None
    last_seen_age_s: int | None = None
    # A stoppage: true when it was seen inside the overlay's window, false when not or when a STATUS row reported it resolved
    # after its last row, ``None`` for a cause that is not one.
    still_occurring: bool | None = None
    resolved_at: datetime | None = None
    # Who is on it, as far as the floor can tell (OMN-23388): ``owned`` when a live lane is on the row, ``unowned`` when
    # none is and the owner map and the lane list were fresh and read, ``unknown`` when none is but either input was
    # stale or unread. Only ``unowned`` says nobody is on it.
    owner_state: Literal["owned", "unowned", "unknown"] = "unowned"
    # Why the owner is unknown, with the read's age; empty unless ``owner_state`` is ``unknown``.
    owner_note: str = ""


class ModelFloorJoinRow(_Frozen):
    """One join as the board draws it (OMN-23081): its state, whether it counts toward ready, who owns it and which side failed."""

    id: str
    label: str
    to_piece: int
    state: str
    golden_age_s: int | None
    error_age_s: int | None
    from_piece: int = 0
    # The join counts toward READY when both of its pieces are in the readiness scope.
    counted: bool = False
    # The join's state is one the overlay lists as passing.
    passes: bool = False
    # The red row of a counted join that does not pass (``join:<id>``); ``None`` for a passing or uncounted join.
    cause_id: str | None = None
    # The join's definition in words, from the overlay: which pieces it joins.
    definition: str = ""
    # Which side failed: ``from``, ``to`` or ``neither`` (both pieces read, no cycle ran it); empty for a passing join.
    failed_side: str = ""
    failed_reason: str = ""
    # Age of the newest chain of the join, golden or error; ``None`` when the join never ran.
    last_run_age_s: int | None = None
    owners: tuple[str, ...] = ()
    lanes: tuple[str, ...] = ()
    # Real work (OMN-23862): age of the newest golden chain that came from real work, ``None`` when none did. The join's
    # ``state`` and ``golden_age_s`` are the probe health, shown apart; only ``real_passes`` counts toward READY.
    real_golden_age_s: int | None = None
    real_passes: bool = False


class ModelFloorShadowLine(_Frozen):
    """What a piece's check would have refused, as its newest shadow row says, with the row's age."""

    label: str
    text: str
    age_s: int
    max_age_s: int
    # The row is older than the age it declared: its numbers are old, and the page says so.
    stale: bool


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
    # The side panel's cells. ``what`` is the overlay's sentence, ``reason`` the reading's own one line without the event
    # ages, ``next_text`` the step the piece's state calls for. The counts are the golden and error chains that ended
    # inside ``window_s``; ``None`` when the events could not be read (unread is never drawn as zero).
    what: str = ""
    reason: str = ""
    next_text: str = ""
    window_s: int = Field(default=86400, gt=0)
    golden_in_window: int | None = None
    error_in_window: int | None = None
    shadows: tuple[ModelFloorShadowLine, ...] = ()
    # READY (OMN-23081): the piece is in the readiness scope, its own reading is WORKING and every counted join at either end
    # passes. ``integration`` says which of the four a piece is; a piece that works and is not ready draws amber.
    ready: bool = False
    in_scope: bool = False
    integration: Literal[
        "ready", "working_not_integrated", "not_working", "not_started"
    ] = "not_started"
    # The ids of the counted joins, at either end of the piece, that do not pass.
    blocking_joins: tuple[str, ...] = ()
    # How many joins count for this piece (both ends in scope).
    counted_joins: int = 0
    # Counted joins that pass on probe health but saw no real work in the window (OMN-23862), and the neighbours on a counted
    # join whose own reading blocks this piece's READY.
    probe_only_joins: tuple[str, ...] = ()
    blocking_neighbours: tuple[int, ...] = ()


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


class ModelFloorReadiness(_Frozen):
    """The count the board reports in place of "working" pieces (OMN-23081): ready pieces, and those that work but are not integrated."""

    in_scope: tuple[int, ...]
    ready: tuple[int, ...]
    working_not_integrated: tuple[int, ...]
    not_working: tuple[int, ...]
    broken_joins: tuple[str, ...]
    text: str
    # Probe health shown apart from integration (OMN-23862): the joins whose probe chains pass, and how many of them saw
    # no real work in the window. Probes never count toward READY.
    probe_health_joins: tuple[str, ...] = ()
    probe_only_joins: tuple[str, ...] = ()
    probe_health_text: str = ""
    # Whether the overlay names the real-work origins (OMN-23862). ``not_configured`` means the old rule applied (a join
    # passes on its health alone) and ``real_work_notice`` says so in one line.
    real_work_rule: Literal["configured", "not_configured"] = "configured"
    real_work_notice: str = ""


class ModelRebuild(_Frozen):
    current_piece: int
    headline: str
    next_text: str
    # The highest piece the ledger's go rulings cover, or ``None`` when the ledger could not be read: unread is not zero.
    go_given_through: int | None
    # The pieces a ruling opened by name beyond the in-order go (OMN-23313); empty when none, or when the ledger was unread.
    go_pieces: tuple[int, ...] = ()


class ModelOwnerInputs(_Frozen):
    """The two inputs of "who is on it", judged by age (OMN-23388); ``None`` on the document is an overlay that judges none."""

    unknown: bool
    # Why, naming each input that is stale or unread with its age; empty when both were fresh and read.
    reason: str = ""
    # Seconds since the owner map was read; ``None`` when it was not read.
    owner_map_age_s: int | None = None
    owner_map_max_age_s: int | None = None
    # Seconds since the lane list was read; ``None`` when it was not read.
    lanes_age_s: int | None = None
    lanes_max_age_s: int | None = None


class ModelRedRule(_Frozen):
    """The reading of the rule: a red row never has work under way on its cause."""

    state: ReadingState
    red_rows: int
    red_with_work_under_way: int
    violations: tuple[str, ...] = ()
    # Every red cause that no overlay owners entry or lab source names an owner for (OMN-22862): the board says nobody
    # is on these by construction, so the ids are shown rather than counted.
    unowned_red: tuple[str, ...] = ()
    # Every red cause no live lane is on, whatever tickets it lists as owners (OMN-23243): a listed owner ticket with no
    # running lane carries nothing, so the rule reads NOT WORKING and names the row instead of counting it as carried.
    uncarried_red: tuple[str, ...] = ()
    # Every red cause no live lane is on while the owner map or the lane list was stale or unread (OMN-23388): the board
    # cannot say nobody is on these, so they are named here and never in ``unowned_red`` or ``uncarried_red``.
    owner_unknown_red: tuple[str, ...] = ()


class ModelHealthReadingUsed(_Frozen):
    """One piece reading the customer health line used, with the age it was judged at (OMN-22923, piece 17b)."""

    piece: int = Field(ge=1, le=18)
    name: str
    state: ReadingState | None
    age_s: int | None
    max_age_s: int | None
    verdict: ReadingVerdict


class ModelFloorHealthLine(_Frozen):
    """The one customer health line: its state, its words, when it was computed and the readings behind it.

    Computed by ``node_product_floor_verdict_compute`` from the piece readings of the same floor record, so a stale
    reading turns the line NOT WORKING and is named in ``named``.
    """

    schema_id: Literal["omni.floor-health/1"] = "omni.floor-health/1"
    state: ReadingState
    line: str = Field(min_length=1)
    generated_at: datetime
    # How old this line may be before the page stops drawing it as current; from the overlay.
    max_age_s: int = Field(gt=0)
    readings: tuple[ModelHealthReadingUsed, ...]
    # One entry per reading that keeps the line from WORKING, each naming the piece and why.
    named: tuple[str, ...] = ()
    oldest_age_s: int | None = None


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
    # How many pieces are ready, not how many work (OMN-23081); ``None`` is a record written before the rule existed.
    readiness: ModelFloorReadiness | None = None
    outside_goal: ModelOutsideGoal = Field(
        default_factory=lambda: ModelOutsideGoal(read=False)
    )
    # The customer health line (OMN-22923, 17b); ``None`` is a record written before the line existed.
    health: ModelFloorHealthLine | None = None
    # The MVP launch block (OMN-23205): 17 readings of the launch bar; ``None`` is a record written before the block existed.
    mvp_launch: ModelMvpLaunchBlock | None = None
    # The board's work list (OMN-23184): every in-scope item that needs help, one row per class, members paged. ``None`` is a
    # record written before the list existed, or an overlay that declares none.
    worklist: tuple[ModelWorklistClass, ...] | None = None
    # The owner map and lane list as judged this cycle (OMN-23388); ``None`` when the overlay declares no judgment.
    owner_inputs: ModelOwnerInputs | None = None
    # Merged work that is not running, from the integration projection (OMN-23899); ``None`` is a record written before the
    # section existed.
    integration: ModelFloorIntegrationSection | None = None


class ModelGoRule(_Frozen):
    """How the go for rebuild pieces is read from the ledger's RULING rows (never a number set by hand).

    A RULING row counts when it is on ``ticket``, written by ``lane``, and its ``question`` cell matches
    ``question_pattern``; the pattern's named groups ``first`` and ``last`` give the pieces it asks about (``last`` is
    optional and defaults to ``first``). The row grants those pieces when its verbatim answer, the quoted last cell,
    matches ``affirmative_pattern``, and revokes them when it does not. The newest ruling that covers a piece decides it.
    The go reaches piece N only when every piece from 1 to N is granted, because the pieces are taken in order.
    """

    ticket: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    lookback_days: int = Field(gt=0)
    question_pattern: str = Field(min_length=1)
    affirmative_pattern: str = Field(min_length=1)
    # A piece opened by subject, apart from the in-order go (OMN-23313): a RULING row of ``lane`` on any of
    # ``open_tickets`` (``ticket`` is read in order above) whose question matches ``open_pattern`` opens the pieces its group ``list`` names, wherever they sit in
    # the order, when the verbatim answer matches ``open_affirmative_pattern``; a later ruling that names a piece and does not
    # match takes it back. The three are declared together or not at all, and a rule without them opens nothing by subject.
    open_tickets: tuple[str, ...] = ()
    open_pattern: str | None = Field(default=None, min_length=1)
    open_affirmative_pattern: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _patterns(self) -> ModelGoRule:
        try:
            question = re.compile(self.question_pattern)
            re.compile(self.affirmative_pattern)
        except re.error as exc:
            raise ValueError(f"a go rule pattern does not compile: {exc}") from exc
        if "first" not in question.groupindex:
            raise ValueError("the question pattern names a group called first")
        if (self.open_pattern is None) != (self.open_affirmative_pattern is None):
            raise ValueError(
                "open_pattern and open_affirmative_pattern are declared together"
            )
        if self.open_pattern is not None and self.open_affirmative_pattern is not None:
            try:
                opened = re.compile(self.open_pattern)
                re.compile(self.open_affirmative_pattern)
            except re.error as exc:
                raise ValueError(f"an open pattern does not compile: {exc}") from exc
            if "list" not in opened.groupindex:
                raise ValueError("the open pattern names a group called list")
        elif self.open_tickets:
            raise ValueError("open_tickets are declared with the open patterns")
        return self


class ModelRowMatch(_Frozen):
    """Which ledger rows count: the row type, the lane, and the cells that must equal these values."""

    row_type: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    cells: dict[str, str] = Field(default_factory=dict)


class ModelPieceEventRule(_Frozen):
    """A piece whose golden and error events are ledger rows instead of emissions: which rows, and who emits them.

    A golden row stands for a start and a success of ``node`` under one correlation id. An error row stands for a start
    and a typed failure, run as a drill when the row says so through ``error_drill_cell``; its failure code is the value
    of ``failure_code_cell`` (or ``failure_default`` when the row has none).
    """

    piece: int = Field(ge=1, le=18)
    node: str = Field(min_length=1)
    golden: ModelRowMatch
    error: ModelRowMatch
    failure_code_cell: str = Field(min_length=1)
    failure_default: str = Field(min_length=1)
    error_drill_cell: str = Field(min_length=1)
    lookback_days: int = Field(gt=0)
    #: The cell of a row that names the correlation id the row's chain runs under, when the row answers another piece's chain
    #: (a read that took the upstream piece's success as its input). A row without the cell, or with it empty, keeps the id the
    #: floor derives from the row itself.
    correlation_cell: str | None = Field(default=None, min_length=1)


class ModelLedgerReadingRule(_Frozen):
    """Which ledger rows are the reading writer's reading rows, and which cells hold what the floor needs of one.

    The reading writer appends one row per piece per window (a STATUS row of its own lane). The floor draws a piece's
    reading from the newest such row of the piece, so the strip, the colour and the staleness rule all read what the
    writer wrote. Nothing here names a piece: ``piece_cell`` holds it. A verdict is written as WORKING, NOT_WORKING or
    UNKNOWN; the floor draws NOT_WORKING as ``NOT WORKING``.
    """

    row_type: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    cells: dict[str, str] = Field(default_factory=dict)
    piece_cell: str = Field(min_length=1)
    verdict_cell: str = Field(min_length=1)
    probed_at_cell: str = Field(min_length=1)
    published_at_cell: str = Field(min_length=1)
    max_age_cell: str = Field(min_length=1)
    # The cell that says why the writer gave the verdict (no producer yet, a source that failed, a stale source); the strip
    # draws it, so a reader sees whether the writer lacks a piece's source or the piece truly fails. ``None`` draws none.
    reason_cell: str | None = None
    lookback_days: int = Field(gt=0)


class ModelStartCriterion(_Frozen):
    """When a rebuild piece starts without the operator's say (RULING 2026-11-07T12:20:46Z): its prerequisites read true.

    The piece is admitted when every piece in ``after_working`` reads WORKING on the floor's own readings. The criterion
    is data of the overlay; no piece number is named in code.
    """

    piece: int = Field(ge=1, le=18)
    after_working: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _prerequisites(self) -> ModelStartCriterion:
        if self.piece in self.after_working or any(
            not 1 <= p <= 18 for p in self.after_working
        ):
            raise ValueError(
                "a start criterion names other pieces between 1 and 18 as its prerequisites"
            )
        return self


class ModelShadowField(_Frozen):
    """One cell of a shadow row the floor shows, under the label it shows it by."""

    cell: str = Field(min_length=1)
    label: str = Field(min_length=1)


class ModelLedgerShadowRule(_Frozen):
    """Which ledger rows are a piece's shadow results, and which cells the floor shows of the newest one.

    A shadow row is a check that logs what it would refuse and holds nothing. The floor shows the newest matching row of the
    rule under its piece, with its age against the max age the row itself declares; it never draws the piece red from one.
    """

    piece: int = Field(ge=1, le=18)
    label: str = Field(min_length=1)
    row_type: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    cells: dict[str, str] = Field(default_factory=dict)
    published_at_cell: str = Field(min_length=1)
    max_age_cell: str = Field(min_length=1)
    fields: tuple[ModelShadowField, ...] = Field(min_length=1)
    lookback_days: int = Field(gt=0)
    # The cell that says whether the check read its input, and the value that says it did. A row whose cell holds any other value
    # shows that value instead of its numbers: a failed run is never drawn as a fresh empty inventory. Both unset shows the numbers.
    outcome_cell: str | None = Field(default=None, min_length=1)
    outcome_ok: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _outcome_pair(self) -> ModelLedgerShadowRule:
        if (self.outcome_cell is None) != (self.outcome_ok is None):
            raise ValueError("outcome_cell and outcome_ok are declared together")
        return self


class ModelRebuildOverlay(_Frozen):
    go: ModelGoRule
    # The piece shown when the go cannot be read; with the go read, the strip names the first admitted piece not yet working.
    current_piece: int = Field(ge=1, le=18)
    step0_pieces: tuple[int, ...] = ()
    # A piece with a start criterion is admitted as soon as its prerequisites read WORKING, with or without a go.
    start_criteria: tuple[ModelStartCriterion, ...] = ()
    lab_state_piece: int = Field(ge=1, le=18)
    # A piece whose own reading does not cover everything its name says also draws from these lab sources: piece -> ids.
    piece_lab_sources: dict[int, tuple[str, ...]] = Field(default_factory=dict)
    # How far back the golden and error events of each piece are counted for the main page's side panel (OMN-22478).
    event_window_s: int = Field(default=86400, gt=0)


class ModelNumbersOverlay(_Frozen):
    landing_goal_per_hour: int = Field(gt=0)
    merge_window_minutes: int = Field(gt=0)
    excluded_pr_classes: tuple[str, ...] = ()
    watcher_max_age_s: int = Field(gt=0)
    record_max_age_s: int = Field(gt=0)


class ModelOwnerInputsOverlay(_Frozen):
    """What makes "who is on it" readable (OMN-23388): the lab source that reads the owner map, and the lane list's bound."""

    # The id of the lab source whose reading is the owner map's (the overlay store source). Its own declared bound is the
    # owner map's maximum age; a source it did not report, or that was not read, is an owner map that was not read.
    owner_map_source: str = Field(min_length=1)
    # The lane list (the floor record's lanes) is older than this many seconds: its owners are unknown.
    lanes_max_age_s: int = Field(gt=0)


class ModelCauseOverlay(_Frozen):
    max_shown: int = Field(gt=0)
    # The delegation:idle row (OMN-23313); ``None`` declares none and the floor draws none.
    delegation_idle: ModelDelegationIdleOverlay | None = None
    titles: dict[str, str] = Field(default_factory=dict)
    owners: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    needs_operator: tuple[str, ...] = ()
    ignore_stoppages: tuple[str, ...] = ()
    pr_classes: dict[str, str] = Field(default_factory=dict)
    # Stoppage causes whose logged cost is an estimate of idle capacity, not lost work: the figure is not shown.
    estimated_cost_stoppages: tuple[str, ...] = ()
    # A stoppage seen within this many seconds of the clock is still occurring; ``None`` does not judge it.
    still_occurring_within_s: int | None = Field(default=None, gt=0)
    # When declared, a row with no lane on it reads owner unknown, not unowned, while the owner map or lane list is stale or
    # unread (OMN-23388); ``None`` judges neither input.
    owner_inputs: ModelOwnerInputsOverlay | None = None


class ModelReadinessOverlay(_Frozen):
    """What READY means for a rebuild piece (OMN-23081, the operator's ruling of 2026-11-08 05:08:55Z).

    A piece is READY when it is in scope, its own reading is WORKING, at least ``min_joins`` joins count for it and every
    counted join reads one of ``passing_join_states``. A join counts when both of its pieces are in ``in_scope_pieces``.
    Every counted join that does not pass is a red row (``join:<id>``) whose owner tickets are the cause overlay's owners.
    ``finding_sides`` says which side of a join a chain finding blames; no value of this kind is in code.
    """

    in_scope_pieces: tuple[int, ...] = Field(min_length=1)
    min_joins: int = Field(ge=1)
    passing_join_states: tuple[str, ...] = Field(min_length=1)
    finding_sides: dict[str, Literal["from", "to"]] = Field(default_factory=dict)
    # Real work (OMN-23862). A join counts toward READY only when its golden chain inside the window came from a chain whose
    # origin is one of these; probe, twin, control, canary, drill and unknown origins are health only. Empty (the key absent
    # from the overlay) means the real-work rule is not configured: the old rule applies and the board says so in one line
    # (``ModelFloorReadiness.real_work_rule``), so this code and the overlay values merge in either order.
    real_origins: tuple[ChainOrigin, ...] = ()
    # A piece is not READY while a neighbour on a counted join has one of these piece statuses (a ``ModelFloorPieceRow.status``).
    # Empty (the key absent) means the neighbour rule is not configured and is not applied.
    blocking_neighbour_statuses: tuple[
        Literal["not_started", "started", "working", "not_working", "unknown"], ...
    ] = ()

    @property
    def real_work_configured(self) -> bool:
        """True when the overlay names the origins that count as real work."""
        return bool(self.real_origins)

    @model_validator(mode="after")
    def _pieces_exist(self) -> ModelReadinessOverlay:
        if any(not 1 <= p <= 18 for p in self.in_scope_pieces):
            raise ValueError("the readiness scope names pieces 1 to 18 only")
        return self


class ModelFloorSimpleOverlay(_Frozen):
    """The overlay of node_floor_simple_compute: the pieces, the joins, the numbers' goal and the cause wording."""

    pieces: tuple[ModelPieceDef, ...] = Field(min_length=18, max_length=18)
    joins: tuple[ModelJoinDef, ...] = Field(min_length=1)
    rebuild: ModelRebuildOverlay
    numbers: ModelNumbersOverlay
    causes: ModelCauseOverlay
    readiness: ModelReadinessOverlay
    piece_events: tuple[ModelPieceEventRule, ...] = ()
    # Where the reading writer's rows are on the ledger; ``None`` reads none (the file route of the runtime alone).
    ledger_readings: ModelLedgerReadingRule | None = None
    # Where each piece's shadow rows are on the ledger; none reads none.
    ledger_shadows: tuple[ModelLedgerShadowRule, ...] = ()
    freshness_max_age_s: int = Field(gt=0)
    error_receipt_max_age_s: int = Field(gt=0)
    error_drill_after_s: int = Field(gt=0)
    chain_deadline_s: int = Field(gt=0)
    details_href: str = Field(min_length=1)
    # The board's work list (OMN-23184); ``None`` declares none and the floor draws none.
    worklist: ModelWorklistOverlay | None = None
    # The integration section's words (OMN-23899); ``None`` declares none and the floor draws the section not configured.
    integration: ModelFloorIntegrationOverlay | None = None

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
    # Why the writer gave this verdict, when the row says (the rule's reason cell); empty otherwise.
    reason: str = ""


class ModelShadowReading(_Frozen):
    """The newest shadow row of one rule: when it was published, how long it stays fresh, and the cells the floor shows."""

    piece: int
    label: str
    published_at: datetime
    max_age_s: int
    # ``(label, value)`` of each cell the rule shows, in the rule's order.
    values: tuple[tuple[str, str], ...]
    # The outcome cell's value when it is not the one that says the input was read; ``None`` when it was (or the rule has none).
    failed_outcome: str | None = None


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
    pr_class_prs: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    # Open pull requests ("repo#number") that cite no ticket, as the work-state projection flags them.
    unticketed_prs: tuple[str, ...] = ()
    # When the friction rollup behind the stoppages was built: the stoppages say nothing newer.
    friction_as_of: datetime | None = None
    stoppages: tuple[ModelFloorCauseIn, ...] = ()
    lanes: tuple[ModelFloorLane, ...] = ()
    lab: ModelLabStateOutput | None
    receipts: ModelFloorReceipts | None
    # How far back the loaded receipt chains reach; a count over a longer window is incomplete, so it is unread.
    receipts_covered_s: int | None = Field(default=None, gt=0)
    readings: dict[int, ModelReadingRow] | None = None
    shadows: tuple[ModelShadowReading, ...] = ()
    # The highest piece with a go, read from the ledger's rulings; ``None`` when the ledger could not be read.
    go_given_through: int | None
    # The pieces a ruling opened by name beyond the in-order go (OMN-23313).
    go_pieces: tuple[int, ...] = ()
    alarms: tuple[ModelFloorAlarmLine, ...] = ()
    sources_read: dict[str, bool] = Field(default_factory=dict)
    alarm_heartbeat_at: datetime | None = None
    outside_goal: ModelOutsideGoal = Field(
        default_factory=lambda: ModelOutsideGoal(read=False)
    )
    # The work list's items as the sources read them; ``None`` is a list that was not read (drawn as an unread input).
    worklist: tuple[ModelWorklistItemIn, ...] | None = None
    # The operator-owned delegation tickets whose start criterion reads true (OMN-23313); ``None`` is a list that was not
    # read (drawn as an unread input), ``()`` is a list that was read and holds none.
    delegation_tickets: tuple[ModelDelegationTicketIn, ...] | None = None
    # The integration projection as this cycle read it (OMN-23899); ``None`` is a cycle that did not read it.
    integration: ModelFloorIntegrationIn | None = None


# --- the alarm ---------------------------------------------------------------------------------------------------------


class ModelAlarmChannelDef(_Frozen):
    """One alarm channel. A ``slack`` channel names, by reference, the secrets it resolves at the effect boundary.

    ``token_ref`` and ``channel_ref`` are the names of the secrets (never their values); ``env_file_env`` names the
    environment variable that holds the path of the operator's bootstrap env file, from which the two named keys are
    read when the process environment does not hold them. The values appear nowhere in a model, a log line or a receipt.
    """

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["unconfigured", "in_memory", "floor_banner", "slack"]
    role: Literal["primary", "second"]
    token_ref: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    channel_ref: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    env_file_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    timeout_s: int = Field(default=10, gt=0)

    @model_validator(mode="after")
    def _slack_refs(self) -> ModelAlarmChannelDef:
        if self.kind == "slack" and not (self.token_ref and self.channel_ref):
            raise ValueError("a slack channel names its token_ref and channel_ref")
        return self


class ModelCanaryPublishOverlay(_Frozen):
    """Where the floor's age and the alarm heartbeat are published for the hosted canary, and the bounds it holds them to.

    The canary runs outside the lab and cannot reach the tailnet, so the cycle publishes two timestamps (and nothing
    else) to a branch the canary checks out. ``remote`` is the repository the branch lives in.
    """

    remote: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    floor_max_age_s: int = Field(gt=0)
    heartbeat_max_age_s: int = Field(gt=0)
    committer_name: str = Field(min_length=1)
    committer_email: str = Field(min_length=1)
    timeout_s: int = Field(default=60, gt=0)


class ModelCanaryStatus(_Frozen):
    """The whole of what is published for the hosted canary: three times and the two bounds, and nothing internal.

    ``floor_generated_at`` and ``alarm_heartbeat_at`` are what the served floor page says about itself. A ``None`` time
    reads as silent to the canary. ``published_at`` is when the cycle published, so a dead publisher is silent too.
    """

    schema_id: Literal["omni.floor-canary-status/1"] = "omni.floor-canary-status/1"
    floor_generated_at: datetime | None
    alarm_heartbeat_at: datetime | None
    published_at: datetime
    floor_max_age_s: int = Field(gt=0)
    heartbeat_max_age_s: int = Field(gt=0)


class ModelAlarmOverlay(_Frozen):
    readback_s: int = Field(gt=0)
    renotify_s: int = Field(gt=0)
    heartbeat_max_age_s: int = Field(gt=0)
    channels: tuple[ModelAlarmChannelDef, ...] = Field(min_length=2)
    canary: ModelCanaryPublishOverlay | None = None

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
    is_test: bool = False


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
    is_test: bool = False


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
    published_at: datetime | None = None
    check_publisher: bool = False


class ModelCanaryVerdict(_Frozen):
    state: Literal[
        "ok", "floor_silent", "alarm_silent", "both_silent", "publisher_silent"
    ]
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
    drill_offline: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    check_overlays_root: str | None = None
    #: The cycle does not publish the canary status: the publish node runs it after the page is served, so the
    #: status carries the age of the page a reader gets (OMN-19998, 17a).
    defer_canary: bool = False
    #: Publish only the canary status, from the page as it is served now, and nothing else.
    canary_only: bool = False


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
    canary: str | None = None
