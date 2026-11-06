# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Portable facts, decisions and events for host worktree reconciliation."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.worktree_reconcile_policy import ModelWorktreeReconcilePolicy


class EnumWorktreeKind(StrEnum):
    LINKED_WORKTREE = "linked_worktree"
    STANDALONE_CLONE = "standalone_clone"
    ORPHAN_DIR = "orphan_dir"


class EnumWorktreeReconcileDecision(StrEnum):
    REMOVE = "remove"
    PIN_AND_REMOVE = "pin_and_remove"
    KEEP = "keep"
    NEEDS_HUMAN = "needs_human"


class EnumDecidedBy(StrEnum):
    RULE = "rule"
    MODEL = "model"


class ModelWorktreeFacts(BaseModel):
    """Only metadata; never file contents or credentials from remote URLs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    path: str
    root: str
    kind: EnumWorktreeKind
    repo_slug: str | None = None
    remote_owner: str | None = None
    repo_visibility: Literal["public", "private", "unknown"] = "unknown"
    branch: str | None = None
    head_sha: str = ""
    detached: bool = False
    dirty_tracked_count: int = Field(default=0, ge=0)
    untracked_nonjunk_count: int = Field(default=0, ge=0)
    unpushed_stash_count: int = Field(default=0, ge=0)
    in_progress_operation: (
        Literal["rebase", "merge", "cherry-pick", "bisect"] | None
    ) = None
    git_locked: bool = False
    head_on_remote: bool = False
    content_merged: bool = False
    # HEAD is exactly the head commit of a merged pull request of this repository,
    # read from the command's merged_pr_command. The commit was pushed and landed, so
    # a squash merge that left the branch's own commits off every remote still proves
    # that nothing here is unpushed. False when no reader is configured or it failed.
    merged_pr_head: bool = False
    commits_not_on_remote: int = Field(default=0, ge=0)
    local_branches_all_on_remote: bool = False
    has_remote: bool = False
    open_pr: bool | None = None
    live_process: bool = False
    live_claim: bool = False
    last_activity_age_hours: float | None = Field(default=None, allow_inf_nan=False)
    size_bytes: int | None = Field(default=None, ge=0)
    orphan_empty: bool = False
    file_names: tuple[str, ...] = ()
    diff_stat: str = ""
    commit_subjects: tuple[str, ...] = Field(default=(), max_length=20)
    probe_errors: tuple[str, ...] = ()

    @property
    def facts_complete(self) -> bool:
        return not self.probe_errors

    @property
    def dirty(self) -> bool:
        return bool(
            self.dirty_tracked_count
            or self.untracked_nonjunk_count
            or self.unpushed_stash_count
        )


class ModelWorktreeDecisionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    decision: EnumWorktreeReconcileDecision
    decided_by: EnumDecidedBy = EnumDecidedBy.RULE
    reasons: tuple[str, ...]
    model_rationale: str | None = None


class ModelWorktreeReconcileRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: tuple[ModelWorktreeFacts, ...]
    policy: ModelWorktreeReconcilePolicy = Field(
        default_factory=ModelWorktreeReconcilePolicy
    )


class ModelWorktreeReconcileDecisions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decisions: tuple[ModelWorktreeDecisionRecord, ...]
    policy: ModelWorktreeReconcilePolicy = Field(
        default_factory=ModelWorktreeReconcilePolicy
    )


class ModelWorktreeReconcileCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    roots: tuple[str, ...]
    extra_clone_roots: tuple[str, ...] = ()
    # Never candidates, whatever they contain: canonical clones, runtime
    # workspaces, the scheduler's own install directory.
    exclude_paths: tuple[str, ...] = ()
    ledger_path: str | None = None
    execute: bool = False
    policy: ModelWorktreeReconcilePolicy = Field(
        default_factory=ModelWorktreeReconcilePolicy
    )
    decider_command: list[str] | None = Field(default=None, min_length=1)
    pin_command: list[str] | None = Field(default=None, min_length=1)
    # Argv with a {slug} placeholder; stdout lists the head commit (40 hex digits) of
    # each merged pull request of that repository, one per line. Unset: no pull request
    # evidence is read and merged_pr_head stays False.
    merged_pr_command: list[str] | None = Field(default=None, min_length=1)
    correlation_id: UUID


class ModelWorktreeReconcileDecidedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str
    facts: ModelWorktreeFacts
    decision: ModelWorktreeDecisionRecord
    outcome: Literal[
        "dry_run", "removed", "pinned_and_removed", "kept", "needs_human", "failed"
    ]
    error: str | None = None


class ModelWorktreeReconcileRunCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str
    started_at: datetime
    finished_at: datetime
    scanned: int = Field(ge=0)
    removed: int = Field(ge=0)
    pinned_and_removed: int = Field(ge=0)
    kept: int = Field(ge=0)
    needs_human: int = Field(ge=0)
    failures: int = Field(ge=0)
    freed_bytes: int = Field(ge=0)
    needs_human_paths: tuple[str, ...]


class ModelWorktreeReconcileRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decided_events: tuple[ModelWorktreeReconcileDecidedEvent, ...]
    completed_event: ModelWorktreeReconcileRunCompletedEvent
    errors: tuple[str, ...] = ()
