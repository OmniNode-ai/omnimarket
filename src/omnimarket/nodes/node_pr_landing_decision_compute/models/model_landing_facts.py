# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelLandingFacts: the input of the landing decision.

One consistent snapshot per tick, read beforehand by effects: GitHub truth for
every PR and companion in scope, the process table probe of every live lease,
the worker result files, the producer's acknowledgements and failures, and the
controller's own state as it last wrote it. The decision itself does no I/O
and reads no clock: ``observed_at`` is the moment the snapshot describes, and
deadlines and grace periods are compared against it.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingCi,
    EnumLandingEngine,
    EnumLandingExternalBlockerKind,
    EnumLandingMergeState,
    EnumLandingPrState,
    EnumLandingPushMode,
    EnumLandingRedClass,
    EnumLandingRefUpdateKind,
    EnumLandingResultKind,
    EnumLandingSuspension,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    KEY_PATTERN,
    PR_KEY_PATTERN,
    REPO_PATTERN,
    SHA_PATTERN,
    ModelLandingControllerState,
    ModelLandingMemberRef,
)


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

    @model_validator(mode="after")
    def _red_needs_class(self) -> ModelLandingPrFacts:
        if self.ci is EnumLandingCi.RED and self.red_class is None:
            raise ValueError("a red head needs its red_class")
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
        ..., pattern=PR_KEY_PATTERN, description="The PR the worker was sent to."
    )
    kind: EnumLandingResultKind
    head_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    push_evidence: tuple[ModelLandingPushEvidence, ...] = ()
    rerun_run_id: int | None = Field(default=None, ge=1)
    blocker_kind: EnumLandingExternalBlockerKind | None = None
    blocker_ref: str | None = None


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
    engine_ladder: tuple[EnumLandingEngine, ...] = Field(
        default=(
            EnumLandingEngine.CLAUDE_SONNET,
            EnumLandingEngine.CLAUDE_OPUS,
            EnumLandingEngine.CODEX_HIGH,
        ),
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
        return self


def _is_repo(value: str) -> bool:
    return re.fullmatch(REPO_PATTERN, value) is not None


__all__: list[str] = [
    "ModelLandingBlockerRef",
    "ModelLandingCompanionFacts",
    "ModelLandingFacts",
    "ModelLandingPolicy",
    "ModelLandingPrFacts",
    "ModelLandingPushEvidence",
    "ModelLandingRebuildAck",
    "ModelLandingRefUpdate",
    "ModelLandingRerunRun",
    "ModelLandingWorkerProbe",
    "ModelLandingWorkerResult",
]
