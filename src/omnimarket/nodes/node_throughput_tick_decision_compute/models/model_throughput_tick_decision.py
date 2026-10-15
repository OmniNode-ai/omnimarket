# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the merge-throughput tick decisions (OMN-20686)."""

from __future__ import annotations

from typing import Literal

from omnibase_core.types import JsonType
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lab_fill import ModelLabFillPrLandFacts, ModelLabFillPrLandPlan

_TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class ModelLabMark(BaseModel):
    """A limited or auth-expired mark file the caller read in the placement directory.

    `absent`: no such file. `unreadable`: the file exists but could not be read or lacks its
    keys. `present`: `until` (and `at` for an auth mark) hold the stamps the file carries.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["absent", "present", "unreadable"] = "absent"
    until: str | None = None
    at: str | None = None


class ModelLabReceipt(BaseModel):
    """One remote-lane runner receipt the caller read, reduced to the fields the finding uses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    started_at: str
    host: str | None = None
    final: bool = False
    status: str | None = None
    pid_alive: bool | None = Field(
        default=None,
        description="Whether the receipt's pid is a live process; None when the receipt has no pid.",
    )
    pid_valid: bool = Field(
        default=True,
        description="False when the receipt's pid is not an integer; such a running receipt is skipped.",
    )
    readings: list[str] = Field(default_factory=list)
    lane: str | None = Field(
        default=None, description="The lane the receipt dispatched (OMN-20840)."
    )
    run_id: str | None = None
    brief: str | None = Field(
        default=None,
        description="The brief path the lane ran; a later receipt of the same brief supersedes a failed one.",
    )
    reason: str | None = Field(
        default=None,
        description="Why the lane ended as it did, as the receipt records it (refusal, failure or error).",
    )
    path: str | None = Field(
        default=None, description="Where the caller read the receipt."
    )
    claim_host: str | None = Field(
        default=None,
        description=(
            "The `host=` cell of the ledger CLAIM row that names this receipt's run (OMN-20851); a running "
            "receipt counts as a running lane only when this and `claim_run_id` are both set."
        ),
    )
    claim_run_id: str | None = Field(
        default=None,
        description="The `run=` cell of that CLAIM row; None when no CLAIM carries a run id.",
    )


class ModelOpenPoint(BaseModel):
    """The open PR count one earlier tick run recorded (OMN-20840)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: str = Field(pattern=_TS)
    open: int = Field(ge=0)


class ModelLabReadingParse(BaseModel):
    """What the placement module made of one placement reading text for one host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parsed: bool
    admission_refusal: str | None = None


class ModelLabAdmission(BaseModel):
    """What the remote-lane runner's live pool read says one host admits right now (OMN-20864).

    The caller reads `landing_placement.read_pool` once per tick and reports the admitted slots per engine and
    the rule that refused an engine; the node decides the finding from them. `slots` is the host's one lane
    slot budget, which a Claude lane and a Codex lane both draw from: the engine slot counts are never added.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    placed: int = Field(
        ge=0, description="Placed units (lanes and landing workers) on the host."
    )
    lanes: int = Field(ge=0, description="Lane placements among the placed units.")
    cap: int = Field(ge=0, description="The host's lane cap.")
    slots: int = Field(
        ge=0, description="Further lanes the host admits, before any engine mark."
    )
    probe_error: str | None = Field(
        default=None,
        description="Why the live probe could not read the host; the host's capacity is then unread.",
    )
    claude_slots: int = Field(
        ge=0, description="`slots` when a Claude lane is admitted, else 0."
    )
    claude_refusal: str | None = None
    codex_slots: int = Field(
        ge=0, description="`slots` when a Codex lane is admitted, else 0."
    )
    codex_refusal: str | None = None


class ModelLabHost(BaseModel):
    """A pool host the caller read, with the marks, parse and admission facts for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    local: bool = False
    cap: int | None = None
    codex_allowed: bool = Field(
        default=False,
        description=(
            "Whether the shared lab host table (or ONEX_CODEX_HOSTS) lets this host run a Codex lane, as "
            "the remote-lane runner's codex_placement.codex_hosts reads it; False reports no Codex "
            "headroom for the host."
        ),
    )
    admission: ModelLabAdmission | None = Field(
        default=None,
        description="The live admission read (OMN-20864); used when the facts set `live_admission`.",
    )
    limited_mark: ModelLabMark = Field(default_factory=ModelLabMark)
    auth_mark: ModelLabMark = Field(default_factory=ModelLabMark)
    parses: dict[str, ModelLabReadingParse] = Field(
        default_factory=dict,
        description=(
            "For each placement reading text of this host in the receipts, whether the placement "
            "module parsed it and the lane admission refusal it names, if any."
        ),
    )


class ModelCodexHeadroom(BaseModel):
    """One host's Codex headroom, read from the same cached placement reading as its Claude headroom.

    The reading carries `codex=login/<sandbox>` and `CODEX-LIMITED-UNTIL=`; the `.limited-<host>` and
    auth-expired marks are Claude's and never enter it. The sandbox word is not judged: the
    `.codex-sandbox-<host>` marks gate landing workers only, and a lane's own run script probes its
    sandbox before Codex starts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    free: int = Field(ge=0, description="Further Codex lanes the host can take now.")
    cap: int | None = None
    running: int | None = None
    refusal: str | None = Field(
        default=None,
        description="Why `free` is zero, when the host cannot take a Codex lane.",
    )
    claude_blocked: str | None = Field(
        default=None,
        description="The Claude mark that keeps Claude lanes off the host (limited or auth expired), if any.",
    )


class ModelLabHeadroomFacts(BaseModel):
    """The remote-lane runner's pool, marks and receipts as the caller read them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    live_admission: bool = Field(
        default=False,
        description=(
            "The caller read the runner's live pool admission (OMN-20864): each host's `admission` decides its "
            "free slots per engine, and the receipts, marks and parses below decide nothing."
        ),
    )
    module_unavailable: bool = False
    unavailable_hosts: list[str] = Field(
        default_factory=list,
        description="Names of non-local host-table rows marked unavailable, in table order.",
    )
    placement_error: str | None = Field(
        default=None,
        description="Why the placement state could not be read, when it could not.",
    )
    hosts: list[ModelLabHost] = Field(default_factory=list)
    receipts: list[ModelLabReceipt] = Field(
        default_factory=list, description="Receipts in the order the caller read them."
    )
    live_marker_hosts: list[str] = Field(
        default_factory=list,
        description="One host name per placement marker whose process is alive.",
    )


class ModelDispatchedLane(BaseModel):
    """A lane the ledger records as dispatched, with the placement facts the caller read for it (OMN-20851).

    A lane counts as running only with a CLAIM that carries `host=` and a run id; a lane recorded as
    dispatched whose runner wrote no placement receipt is unplaced.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    dispatched_at: str = Field(
        pattern=_TS, description="When the ledger recorded the dispatch."
    )
    claim_host: str | None = Field(
        default=None, description="The `host=` cell of the lane's CLAIM row, if any."
    )
    claim_run_id: str | None = Field(
        default=None, description="The `run=` cell of the lane's CLAIM row, if any."
    )
    placement_receipt: bool = Field(
        default=False,
        description="Whether the runner wrote a placement receipt for this lane.",
    )


class ModelVenvDivergence(BaseModel):
    """A dispatch venv that differs between lab hosts, and since when (OMN-20851)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: str = Field(
        description="The diverged surface, e.g. `venv:omnimarket`; names the package set that differs."
    )
    hosts: list[str] = Field(
        min_length=2, description="The hosts whose dispatch venvs disagree."
    )
    diverged_since: str = Field(
        pattern=_TS,
        description="When the caller first read this divergence; unchanged while it persists.",
    )
    detail: str = ""


class ModelTickAlarmVerdict(BaseModel):
    """The typed verdict record of one ALARM, written to the tick's declared evidence file (OMN-20851)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["ALARM"] = "ALARM"
    loop: str = Field(
        description="The finding name, e.g. `unplaced-lane:<lane>` or `venv-divergence:<surface>`."
    )
    since: str = Field(
        pattern=_TS, description="When the condition began (dispatch or divergence)."
    )
    age_minutes: float = Field(ge=0)
    why: str
    fix: str


RepoStatusVerdict = Literal[
    "merging_normally",
    "waiting_on_ci",
    "base_branch_red",
    "merge_queue_ejecting",
    "shared_red_unowned",
    "parked_or_escalated",
]


class ModelRepoStatusUnit(BaseModel):
    """A repository status check the caller holds open from an earlier tick (OMN-19998)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    opened_at: str = Field(pattern=_TS)
    verdict: RepoStatusVerdict


class ModelRepoStatusFacts(BaseModel):
    """What the caller passes for the per-repository status check (OMN-19998).

    Operator RULING 2026-10-15T00:42:51Z lane=orchestrator-bb1a: a repository with open PRs and nothing
    merged in the window gets its status checked every tick. The check reads only the watcher state,
    state.json and tick lines already in the request; `open_units` dedupes the verdict events.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_minutes: int = Field(
        default=60,
        gt=0,
        description="A repository with no merge in this many minutes is checked.",
    )
    open_units: list[ModelRepoStatusUnit] = Field(
        default_factory=list,
        description="The verdicts the previous tick returned; a repository with one open emits no new event "
        "unless its verdict changed.",
    )


class ModelRepoStatusVerdict(BaseModel):
    """One repository's status-check verdict (OMN-19998), deterministic over the facts the caller read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    verdict: RepoStatusVerdict
    open_prs: int = Field(
        ge=0, description="Open non-draft PRs the watcher holds for the repository."
    )
    merges_in_window: int = Field(ge=0)
    window_minutes: int = Field(gt=0)
    check: str | None = Field(
        default=None,
        description="The check the verdict names: the shared red, the base red or the ejecting base red.",
    )
    reason: str = ""
    queue_wait_minutes: float | None = Field(
        default=None,
        description="Minutes the oldest pending check has waited, for waiting_on_ci.",
    )
    prs: list[str] = Field(
        default_factory=list, description="The PRs (`repo#n`) the verdict names."
    )
    opened_at: str = Field(
        pattern=_TS, description="When the repository's unit opened."
    )
    event: bool = Field(
        description="True when the caller emits this verdict as an event: no unit was open for the "
        "repository, or the open unit's verdict differs."
    )


class ModelThroughputTickRequest(BaseModel):
    """Facts the caller read from the landing controller's files, launchd and the PR watcher.

    The handler reads nothing and has no clock; every field is a fact the caller observed.
    A fact the caller could not read is `None` or an error string, never a guess.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: str = Field(pattern=_TS)
    controller_max_minutes: int = Field(default=15, ge=0)
    controller_run_max_minutes: int = Field(default=30, ge=0)
    watcher_max_minutes: int = Field(default=15, ge=0)
    min_merges_per_hour: int = Field(default=5, ge=0)

    launchctl: Literal["loaded", "not_loaded", "unavailable"]
    launchctl_error: str | None = None
    launchctl_stdout: str = ""
    pid_etime: str | None = Field(
        default=None,
        description="Stripped `ps -o etime=` output for the launchd PID, None when ps failed.",
    )
    ticks: list[dict[str, JsonType]] = Field(
        default_factory=list,
        description="The newest complete controller tick lines, newest first, at most two.",
    )
    ticks_error: str | None = Field(
        default=None,
        description="Why ticks.jsonl could not be read, when it could not.",
    )
    heartbeat_text: str | None = None
    heartbeat_write_error: str | None = Field(
        default=None,
        description=(
            "`<path>: <error>` when the caller could not write the tick heartbeat file; "
            "reported as the first finding."
        ),
    )
    state_json: JsonType | None = Field(
        default=None,
        description="The controller's state.json, parsed; None when unreadable.",
    )

    watcher_path: str | None = None
    watcher_state: JsonType | None = None
    watcher_read_error: str | None = None

    floors_per_repo: dict[str, float] | None = None
    floors_error: str | None = None
    policy_load_error: str | None = None
    policy_error: str = ""
    land_like_operator: list[str] = Field(default_factory=list)

    lab_headroom: ModelLabHeadroomFacts | None = Field(
        default=None,
        description="Lab-headroom facts; None skips the finding (and its `checked` entry).",
    )

    pr_land: ModelLabFillPrLandFacts | None = Field(
        default=None,
        description=(
            "Open PRs with their landing facts and hold source (OMN-20864): the lab-headroom FIX names "
            "pr-land lanes for idle slots on parked, escalated or unowned-red PRs, by the rule lab-fill "
            "selection runs; None leaves the FIX as it was."
        ),
    )

    park_max_hours: float = Field(
        default=2.0,
        ge=0,
        description=(
            "A park over a repository under its floor with zero merges in the window covers it as a NOTE "
            "only while it ends within this many hours; a longer park is MISSING (OMN-20840)."
        ),
    )
    dispatch_window_hours: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Receipts started this many hours back that ended without running are findings; None skips "
            "the dispatches finding (and its `checked` entry)."
        ),
    )
    open_history: list[ModelOpenPoint] | None = Field(
        default=None,
        description=(
            "Open counts earlier tick runs recorded, oldest first; None skips the open-trend finding "
            "(and its `checked` entry)."
        ),
    )
    open_rise_pct: float = Field(default=10.0, ge=0)
    open_history_max_age_hours: float = Field(
        default=3.0,
        gt=0,
        description="Points older than this belong to another session and are not a trend.",
    )

    dispatched_lanes: list[ModelDispatchedLane] | None = Field(
        default=None,
        description=(
            "Lanes the ledger records as dispatched, with the placement facts read for each; None skips "
            "the unplaced-lane finding (and its `checked` entry) (OMN-20851)."
        ),
    )
    unplaced_lane_alarm_minutes: int = Field(
        default=10,
        ge=0,
        description="A dispatched lane with no placement receipt for longer than this is an ALARM.",
    )
    venv_divergences: list[ModelVenvDivergence] | None = Field(
        default=None,
        description=(
            "Cross-host dispatch-venv divergences the caller read; None skips the venv-divergence finding "
            "(and its `checked` entry) (OMN-20851)."
        ),
    )
    venv_divergence_alarm_minutes: int = Field(
        default=30,
        ge=0,
        description="A divergence older than this is an ALARM; a younger one is a NOTE.",
    )
    repo_status: ModelRepoStatusFacts | None = Field(
        default=None,
        description=(
            "Facts for the per-repository status check (OMN-19998); None skips the check (and its "
            "`checked` entry)."
        ),
    )


class ModelThroughputTickResult(BaseModel):
    """The tick's finding lines and its status line, exactly as the retired script printed them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: list[str]
    status_line: str
    missing: list[str]
    unknown: list[str]
    checked: list[str]
    alarms: list[ModelTickAlarmVerdict] = Field(
        default_factory=list,
        description=(
            "Every ALARM verdict, for the caller to print, write into the tick's evidence file and exit "
            "non-zero on (OMN-20851); an ALARM is never also listed in `missing` or `unknown`."
        ),
    )
    exit_code: int = Field(ge=0, le=1)
    open_count: int | None = Field(
        default=None,
        description="The open PR count read from a fresh watcher state, for the caller to record; None when unread.",
    )
    codex_headroom: list[ModelCodexHeadroom] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
        description=(
            "Codex headroom of every Codex-capable pool host with a fresh placement reading, separate "
            "from the Claude headroom the lab-headroom lines report; a host Claude cannot use that has "
            "free Codex slots is also a MISSING lab-headroom line. Absent from the dump when empty."
        ),
    )
    pr_land: ModelLabFillPrLandPlan | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "The idle-slot pr-land fallback the lab-headroom FIX names (OMN-20864); None without PR-land "
            "facts or with the lab's capacity unread, and then absent from the dump, so a caller that "
            "sends no PR-land facts reads the result it read before."
        ),
    )
    repo_status: list[ModelRepoStatusVerdict] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
        description=(
            "One status-check verdict per repository with open PRs and no merge in the window (OMN-19998); "
            "absent from the dump when empty."
        ),
    )
