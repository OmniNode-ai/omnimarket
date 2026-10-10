# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelLandingFacts: the input of the landing decision.

One consistent snapshot per tick, read beforehand by effects: GitHub truth for
every PR and companion in scope, the process table probe of every live lease,
the worker result files, the producer's acknowledgements and failures, and the
controller's own state as it last wrote it. The decision itself does no I/O
and reads no clock: ``observed_at`` is the moment the snapshot describes, and
deadlines and grace periods are compared against it.

The shared-cause inputs (each red check's failure annotation, when a gate
began, the fixer HOLD rows, the floor alarm's breach set, the CLAIMs that name
a cause, RELEASE rows naming a parked cause) are all optional with empty
defaults, so a snapshot without them decides exactly as before.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingCi,
    EnumLandingEngine,
    EnumLandingExternalBlockerKind,
    EnumLandingGateReason,
    EnumLandingMergeState,
    EnumLandingPrState,
    EnumLandingPushMode,
    EnumLandingRedClass,
    EnumLandingRefUpdateKind,
    EnumLandingResultKind,
    EnumLandingSuspension,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    CAUSE_KEY_PATTERN,
    KEY_PATTERN,
    PR_KEY_PATTERN,
    REPO_PATTERN,
    SHA_PATTERN,
    SUBJECT_PATTERN,
    ModelLandingControllerState,
    ModelLandingMemberRef,
)

FIXER_HOLD_ALL = "all"


class ModelLandingBlockerRef(BaseModel):
    """One ref that can block a PR, with the state it was read in (R3, R8).

    A merge-order parent, a stacked base, a HOLD, RELEASE or RULING row naming
    the PR, or the ref an ``external_blocker`` result named. The blocker
    fingerprint is the hash of these, sorted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str = Field(..., min_length=1)
    state: str = Field(..., min_length=1)
    head_sha: str | None = Field(default=None, pattern=SHA_PATTERN)


class ModelLandingRefUpdate(BaseModel):
    """GitHub's record of one update to the PR's head ref."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    before_sha: str = Field(..., pattern=SHA_PATTERN)
    after_sha: str = Field(..., pattern=SHA_PATTERN)
    kind: EnumLandingRefUpdateKind
    at: datetime


class ModelLandingRerunRun(BaseModel):
    """A workflow run re-run on the PR (for a rerun ``fix_attempt``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int = Field(..., ge=1)
    head_sha: str = Field(..., pattern=SHA_PATTERN)
    created_at: datetime


class ModelLandingPrFacts(BaseModel):
    """GitHub truth for one product PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    head_sha: str = Field(..., pattern=SHA_PATTERN)
    state: EnumLandingPrState
    ci: EnumLandingCi
    red_class: EnumLandingRedClass | None = Field(
        default=None, description="Set when ci is red: what clears it."
    )
    red_checks: tuple[str, ...] = Field(
        default=(), description="Names of the red check-runs on the head."
    )
    cancelled_checks: tuple[str, ...] = Field(
        default=(),
        description=(
            "Names of the check-runs on the head whose newest copy is cancelled "
            "(not failed). With merge_state blocked, no red_checks and ci not "
            "pending, a stale cancelled copy holds the merge: refresh once."
        ),
    )
    merge_state: EnumLandingMergeState = EnumLandingMergeState.CLEAN
    suspensions: tuple[EnumLandingSuspension, ...] = ()
    collaborator: bool = Field(
        default=False, description="A collaborator's PR: never eligible (R2 excluded)."
    )
    runtime: bool = Field(
        default=False, description="Needs the runtime token to land (R6)."
    )
    process_fix: bool = Field(
        default=False, description="Carries the process-fix tag (R4)."
    )
    created_at: datetime
    parents: tuple[str, ...] = Field(
        default=(),
        description="Merge-order parents and a stacked base PR, as repo#pr (R3).",
    )
    open_parents: tuple[str, ...] = Field(
        default=(), description="The subset of parents still open this tick."
    )
    blocker_refs: tuple[ModelLandingBlockerRef, ...] = ()
    ref_updates: tuple[ModelLandingRefUpdate, ...] = ()
    rerun_runs: tuple[ModelLandingRerunRun, ...] = ()
    auto_merge_head: str | None = Field(
        default=None,
        pattern=SHA_PATTERN,
        description="The head auto-merge is armed on, if armed.",
    )
    red_annotations: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Red check name -> its failure annotation text on this head; a red "
            "check absent here is unread and never clusters."
        ),
    )
    gate_since: datetime | None = Field(
        default=None,
        description="When the current gate suspension began on this head.",
    )
    gate_reasons: tuple[EnumLandingGateReason, ...] = Field(
        default=(),
        description=(
            "Which gates the gate suspension stands for. A gate suspension with "
            "none is refused, and so are reasons with no gate suspension."
        ),
    )

    @model_validator(mode="after")
    def _red_needs_class(self) -> ModelLandingPrFacts:
        if self.ci is EnumLandingCi.RED and self.red_class is None:
            raise ValueError("a red head needs its red_class")
        gated = EnumLandingSuspension.GATE in self.suspensions
        if gated and not self.gate_reasons:
            raise ValueError("a gate suspension needs at least one named gate_reason")
        if self.gate_reasons and not gated:
            raise ValueError("gate_reasons without a gate suspension")
        if not set(self.open_parents) <= set(self.parents):
            raise ValueError("open_parents must be a subset of parents")
        return self


class ModelLandingCompanionFacts(BaseModel):
    """GitHub truth for one change-control companion, with its typed membership."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    head_sha: str = Field(..., pattern=SHA_PATTERN)
    state: EnumLandingPrState
    ci: EnumLandingCi
    members: tuple[ModelLandingMemberRef, ...] = Field(..., min_length=1)
    rebuild_key: str | None = Field(
        default=None,
        pattern=KEY_PATTERN,
        description="The idempotency key K the producer minted it for, if any.",
    )
    ref_updates: tuple[ModelLandingRefUpdate, ...] = ()
    rerun_runs: tuple[ModelLandingRerunRun, ...] = ()


class ModelLandingWorkerProbe(BaseModel):
    """The process table read for one lease (R7).

    ``group_alive``: a signal-0 probe of the worker's process group found a
    process. ``tagged_alive``: a scan found a process carrying the worker's
    tag anywhere, including a child that left the group with setsid.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lease_id: int = Field(..., ge=1)
    group_alive: bool
    tagged_alive: bool


class ModelLandingPushEvidence(BaseModel):
    """One push a worker made, as its result file reports it (R1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str = Field(..., min_length=1)
    expected_old_head: str | None = Field(default=None, pattern=SHA_PATTERN)
    new_head: str = Field(..., pattern=SHA_PATTERN)
    mode: EnumLandingPushMode


class ModelLandingWorkerResult(BaseModel):
    """One worker result file, unread until this tick."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lease_id: int = Field(..., ge=1)
    pr: str = Field(
        ...,
        pattern=SUBJECT_PATTERN,
        description="The PR, or the cause key, the worker was sent to.",
    )
    kind: EnumLandingResultKind
    head_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    push_evidence: tuple[ModelLandingPushEvidence, ...] = ()
    rerun_run_id: int | None = Field(default=None, ge=1)
    blocker_kind: EnumLandingExternalBlockerKind | None = None
    blocker_ref: str | None = None
    fix_ref: str | None = Field(
        default=None,
        pattern=PR_KEY_PATTERN,
        description="cause_fix_submitted: the one fix PR at the source.",
    )
    fix_head: str | None = Field(
        default=None, pattern=SHA_PATTERN, description="cause_fix_submitted: its head."
    )
    reason: str | None = Field(default=None, description="cause_not_shared: why.")


class ModelLandingCauseOwner(BaseModel):
    """A live CLAIM that names a cause (never a per-PR CLAIM).

    It owns the cause whose key is ``cause``, or, with no key, the cause in
    ``repo`` that carries ``check``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(..., min_length=1)
    repo: str = Field(..., pattern=REPO_PATTERN)
    check: str | None = Field(default=None, min_length=1)
    cause: str | None = Field(default=None, pattern=CAUSE_KEY_PATTERN)

    @model_validator(mode="after")
    def _names_a_cause(self) -> ModelLandingCauseOwner:
        if self.check is None and self.cause is None:
            raise ValueError("a cause owner names the cause key or its check")
        return self


class ModelLandingCauseRelease(BaseModel):
    """A RELEASE row naming a parked cause; it ends a park that began before it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cause: str = Field(..., pattern=CAUSE_KEY_PATTERN)
    at: datetime


class ModelLandingCauseEscalation(BaseModel):
    """An operator MSG row already on the ledger for a parked cause.

    The row carries the dedupe key ``<cause key>@<parked_until>``. The ledger
    row, not the controller's state file, is the source of truth for "this park
    episode was escalated": a crash between the MSG append and the state write
    loses the state file's copy of the key and not the row (model finding LC-F3,
    omnibase_internal#144).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cause: str = Field(..., pattern=CAUSE_KEY_PATTERN)
    at: datetime
    dedupe_key: str = Field(..., min_length=1)


class ModelLandingRepoChecks(BaseModel):
    """The checks red on one repo's base head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., pattern=REPO_PATTERN)
    checks: tuple[str, ...] = ()


class ModelLandingRebuildAck(BaseModel):
    """The producer accepted a delivery of key K (written after delivery)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(..., pattern=KEY_PATTERN)
    run_id: str = Field(..., min_length=1)


class ModelLandingPolicy(BaseModel):
    """The fixed numbers the rules use. Declared, never inferred."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_workers: int = Field(default=6, ge=0, description="Concurrent workers (D5).")
    load_pause_threshold: float = Field(
        default=20.0, gt=0, description="No dispatch while load1 is above this (D5)."
    )
    exit_grace_seconds: int = Field(
        default=120, ge=0, description="After a recorded result, before the kill (R7)."
    )
    lease_seconds: int = Field(default=3600, ge=1, description="A lease's deadline.")
    max_rebuild_attempts: int = Field(default=3, ge=1, description="Per key (R2).")
    rebuild_visibility_ticks: int = Field(
        default=3,
        ge=1,
        description="A delivered key with no companion after this many ticks has failed.",
    )
    rebuild_backoff_ticks: tuple[int, ...] = Field(
        default=(1, 2), min_length=1, description="Backoff before retry n (R2)."
    )
    max_stale_refreshes: int = Field(
        default=2,
        ge=1,
        description="Update-branch refreshes per PR for stale cancelled copies.",
    )
    stale_gate_seconds: int = Field(
        default=600,
        ge=0,
        description=(
            "A gate whose premise is a red head, on a green, CLEAN head, holds "
            "nothing once older than this (two 300 s controller ticks)."
        ),
    )
    engine_ladder: tuple[EnumLandingEngine, ...] = Field(
        default=(
            EnumLandingEngine.CLAUDE_SONNET,
            EnumLandingEngine.CLAUDE_OPUS,
            EnumLandingEngine.CODEX_HIGH,
        ),
        min_length=1,
    )
    cluster_min_members: int = Field(
        default=3, ge=2, description="PRs sharing one (check, signature) to cluster."
    )
    cluster_min_members_floor: int = Field(
        default=2, ge=2, description="The same, in a repo under its landing floor."
    )
    wait_stall_minutes: int = Field(
        default=60, ge=0, description="A gate older than this joins clusters."
    )
    cluster_absorb_ratio: float = Field(
        default=0.8,
        gt=0,
        le=1,
        description="A cluster this much inside a chosen cause is absorbed into it.",
    )
    cluster_excluded_checks: tuple[str, ...] = Field(
        default=(
            "CI Summary",
            "Hostile Reviewer (adversarial gate)",
            "Hostile Review Gate",
        ),
        description="Follower checks and the reviewer pool: never clustered.",
    )
    max_cause_workers: int = Field(default=4, ge=0, description="Fleet-wide.")
    max_cause_workers_per_repo: int = Field(default=2, ge=0)
    max_workers_per_repo: int = Field(
        default=6, ge=0, description="Workers of any kind in one repo."
    )
    cause_attempt_budget: int = Field(
        default=2, ge=1, description="Attempts per cause per park episode."
    )
    cause_spawn_failure_budget: int = Field(
        default=3, ge=1, description="Exits with no result that park the cause."
    )
    cause_spawn_failure_seconds: int = Field(
        default=600,
        ge=0,
        description="An exit with no result this soon after dispatch is not an attempt.",
    )
    cause_lease_seconds: int = Field(default=5400, ge=1)
    cause_park_hours: int = Field(default=12, ge=1)
    cause_engine_ladder: tuple[EnumLandingEngine, ...] = Field(
        default=(EnumLandingEngine.CLAUDE_OPUS, EnumLandingEngine.CLAUDE_OPUS),
        min_length=1,
    )


class ModelLandingFacts(BaseModel):
    """Everything one tick's decision reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick: int = Field(
        ..., ge=1, description="This tick's number; one more than the last."
    )
    observed_at: datetime
    policy: ModelLandingPolicy = Field(default_factory=ModelLandingPolicy)
    state: ModelLandingControllerState = Field(
        default_factory=ModelLandingControllerState
    )
    prs: tuple[ModelLandingPrFacts, ...] = ()
    companions: tuple[ModelLandingCompanionFacts, ...] = ()
    probes: tuple[ModelLandingWorkerProbe, ...] = ()
    results: tuple[ModelLandingWorkerResult, ...] = ()
    rebuild_acks: tuple[ModelLandingRebuildAck, ...] = ()
    producer_failures: tuple[str, ...] = Field(
        default=(), description="Keys whose producer run concluded in failure."
    )
    producer_release: str = Field(
        default="", description="The producer's installed release."
    )
    drain_requested: tuple[str, ...] = Field(
        default=(), description="Repos the operator started handing over."
    )
    load1: float = Field(default=0.0, ge=0)
    available_engines: tuple[EnumLandingEngine, ...] = (
        EnumLandingEngine.CLAUDE_SONNET,
        EnumLandingEngine.CLAUDE_OPUS,
        EnumLandingEngine.CODEX_HIGH,
    )
    fixer_hold: tuple[str, ...] = Field(
        default=(),
        description=(
            "Repos under a fixer HOLD row, or 'all': no worker is dispatched "
            "there and every live lease there is revoked."
        ),
    )
    floor_breached_repos: tuple[str, ...] = Field(
        default=(), description="Repos under their landing floor (the floor alarm)."
    )
    cause_owners: tuple[ModelLandingCauseOwner, ...] = ()
    cause_releases: tuple[ModelLandingCauseRelease, ...] = ()
    cause_escalations: tuple[ModelLandingCauseEscalation, ...] = Field(
        default=(),
        description=(
            "Operator MSG rows already on the ledger for a parked cause (LC-F3): "
            "a park whose episode a row covers emits no second escalation."
        ),
    )
    base_red_checks: tuple[ModelLandingRepoChecks, ...] = Field(
        default=(), description="Checks red on each repo's base head."
    )

    @model_validator(mode="after")
    def _keys_unique(self) -> ModelLandingFacts:
        prs = [p.pr for p in self.prs]
        comps = [c.pr for c in self.companions]
        if len(set(prs)) != len(prs) or len(set(comps)) != len(comps):
            raise ValueError("each PR and companion appears once")
        if set(prs) & set(comps):
            raise ValueError("a companion is listed under companions only")
        if self.tick <= self.state.last_tick:
            raise ValueError("tick must be later than the state's last tick")
        for repo in self.drain_requested:
            if not _is_repo(repo):
                raise ValueError(f"drain_requested entry {repo!r} is not owner/name")
        for repo in self.floor_breached_repos:
            if not _is_repo(repo):
                raise ValueError(
                    f"floor_breached_repos entry {repo!r} is not owner/name"
                )
        for scope in self.fixer_hold:
            if scope != FIXER_HOLD_ALL and not _is_repo(scope):
                raise ValueError(f"fixer_hold entry {scope!r} is not owner/name or all")
        return self


def _is_repo(value: str) -> bool:
    return re.fullmatch(REPO_PATTERN, value) is not None


__all__: list[str] = [
    "FIXER_HOLD_ALL",
    "ModelLandingBlockerRef",
    "ModelLandingCauseOwner",
    "ModelLandingCauseRelease",
    "ModelLandingCompanionFacts",
    "ModelLandingFacts",
    "ModelLandingPolicy",
    "ModelLandingPrFacts",
    "ModelLandingPushEvidence",
    "ModelLandingRebuildAck",
    "ModelLandingRefUpdate",
    "ModelLandingRepoChecks",
    "ModelLandingRerunRun",
    "ModelLandingWorkerProbe",
    "ModelLandingWorkerResult",
]
