# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and records for the per-session defect counters.

The caller reads the local pull request watcher snapshot, the check-run
history it keeps per head and the rolling ledger, and hands the node typed
slices of them. The node makes no GitHub call and reads no file.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# Where the caller reads check-run history per head: the local pull request
# watcher snapshot, never GitHub. `ci.sha` is the head, `ci.runs` rows are
# [name, status, conclusion, completed_at] and `ci.red_facts` rows are
# [name, conclusion, run_id, completed_at]; required contexts come from
# `settings[<repo>].branches[<base>].contexts` and `.rule_contexts`.
CHECK_RUN_HISTORY_SOURCE = (
    'pr-watcher-state.json prs["<repo>#<n>"].ci (sha, runs, red_facts)'
)

Nonempty = Annotated[str, Field(min_length=1)]

NondefectClass = Literal[
    "change_control_cascade",
    "draft_era_residue",
    "replayed_rerun",
    "cancelled_producer",
    "stale_copy",
    "follower",
    "base_inherited",
    "runner_timeout",
    "rate_limit",
]

DefectKind = Literal[
    "red_check", "revert", "correction", "retracted_claim", "pull_request"
]
Finality = Literal["FINAL", "PROVISIONAL"]
RateStatus = Literal["rate", "no denominator"]
PrState = Literal["merged", "closed_unmerged", "open"]


class ModelCheckRun(BaseModel):
    """One check-run copy on a head, as the watcher snapshot's `ci` row keeps it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Nonempty
    conclusion: str
    completed_at: AwareDatetime | None = None
    run_created_at: AwareDatetime | None = None
    run_attempt: int = Field(default=1, ge=1)
    failure_kind: Literal["rate_limit", "runner_timeout"] | None = None


class ModelHead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sha: Nonempty
    checks: tuple[ModelCheckRun, ...] = ()


class ModelPullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ref: Nonempty
    session_id: Nonempty
    state: PrState
    opened_at: AwareDatetime
    merged_at: AwareDatetime | None = None
    closed_at: AwareDatetime | None = None
    ready_at: AwareDatetime | None = None
    required_checks: tuple[Nonempty, ...] = ()
    base_red_checks: tuple[Nonempty, ...] = ()
    heads: tuple[ModelHead, ...] = ()

    @model_validator(mode="after")
    def merged_has_time(self) -> ModelPullRequest:
        if self.state == "merged" and self.merged_at is None:
            raise ValueError("A merged pull request needs merged_at")
        return self


class ModelRevert(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reverting_pr: Nonempty
    reverted_pr: Nonempty
    merged_at: AwareDatetime


class ModelLedgerRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stamp: AwareDatetime
    row_type: Nonempty
    lane: Nonempty
    corrects: AwareDatetime | None = None
    pr_ref: str | None = None


class ModelSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: Nonempty
    lane_family: Nonempty
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    boundaries: tuple[AwareDatetime, ...] = ()

    @model_validator(mode="after")
    def ordered(self) -> ModelSession:
        if self.ends_at < self.starts_at:
            raise ValueError("A session cannot end before it starts")
        if list(self.boundaries) != sorted(set(self.boundaries)):
            raise ValueError("Boundaries must be strictly increasing")
        return self


class ModelDefectCountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    as_of: AwareDatetime
    window_seconds: int = Field(gt=0)
    follow_up_days: int = Field(default=7, gt=0)
    sessions: tuple[ModelSession, ...]
    pull_requests: tuple[ModelPullRequest, ...] = ()
    reverts: tuple[ModelRevert, ...] = ()
    ledger_rows: tuple[ModelLedgerRow, ...] = ()

    @model_validator(mode="after")
    def unique(self) -> ModelDefectCountRequest:
        ids = [s.session_id for s in self.sessions]
        refs = [p.ref for p in self.pull_requests]
        if len(ids) != len(set(ids)) or len(refs) != len(set(refs)):
            raise ValueError("Session ids and pull request refs must be unique")
        return self


class ModelDefect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    identity: Nonempty
    kind: DefectKind
    pr_ref: str | None
    at: datetime
    signals: tuple[str, ...]
    window_id: str | None


class ModelExcludedRed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pr_ref: str
    head_sha: str
    check: str
    nondefect_class: NondefectClass


class ModelRedHead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    head_sha: str
    checks: tuple[str, ...]


class ModelUnmergedPr(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ref: str
    red_heads: tuple[ModelRedHead, ...]


class ModelWindowCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    window_id: str
    boundary: datetime
    side: Literal["before", "after"]
    start: datetime
    end: datetime
    defects: int
    merged_prs: int
    rate: float | None
    rate_status: RateStatus
    finality: Finality


class ModelSessionDefects(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: str
    merged_prs: int
    defect_count: int
    rate: float | None
    rate_status: RateStatus
    finality: Finality
    defects: tuple[ModelDefect, ...]
    windows: tuple[ModelWindowCount, ...]
    outside_window_defects: int
    excluded_reds: tuple[ModelExcludedRed, ...]
    unmerged_prs: tuple[ModelUnmergedPr, ...]


class ModelDefectCountResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    as_of: datetime
    sessions: tuple[ModelSessionDefects, ...]
