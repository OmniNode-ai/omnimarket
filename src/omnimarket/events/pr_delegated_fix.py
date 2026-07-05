# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared event models for the delegated (non-Claude) PR fix path (OMN-13940).

These live in the shared ``omnimarket.events`` package — not inside
``node_pr_delegated_fix_effect`` — so both that node (the consumer of the
command / producer of the result) and ``node_pr_lifecycle_fix_effect`` (whose
``adapter_delegated_fix.DelegatedFixAdapter`` bridges into it) import them
without a cross-node reach-in. See ``tests/test_no_cross_node_reach_in.py``
and the omnimarket CLAUDE.md rule: "Promote shared types instead."

Related:
    - OMN-13940: WS-D/D2 merge-sweep delegation harness Slice 0.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt


class ModelDelegatedFixCommand(BaseModel):
    """Command to run the Slice 0 (deterministic) delegated fix.

    Slice 0 (WS-D/D2, OMN-13940) never calls an LLM — ``delegation_model`` on
    the result is always the deterministic tool identity. Slice 1 adds a
    ``task_type="document"`` path through ``HandlerDelegateSkill`` for
    docstring/comment-only diffs; this command shape is additive-stable
    across both slices.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Fix run correlation ID.")
    repo: str = Field(..., description="GitHub repo slug (owner/repo).")
    pr_number: int = Field(..., description="PR number to remediate.", gt=0)
    ticket_id: str | None = Field(
        default=None, description="Linear ticket ID for worktree/trailer context."
    )
    block_reason: str = Field(
        ..., description="Originating block_reason, for logging/commit trailer."
    )
    changed_files: list[str] = Field(
        default_factory=list,
        description="PR changed-file paths as known at dispatch time (defense-in-depth denylist re-check).",
    )
    diff_total_lines: NonNegativeInt = Field(
        default=0, description="Total additions + deletions known at dispatch time."
    )
    worktree_path: str | None = Field(
        default=None,
        description=(
            "Explicit worktree path. When absent the handler resolves/creates "
            "one under $OMNI_WORKTREES/<ticket_id>/<repo_basename>."
        ),
    )
    dry_run: bool = Field(default=False, description="Run without pushing.")
    run_id: str = Field(
        default_factory=lambda: uuid4().hex[:12],
        description="Unique id embedded in the commit trailer for traceability.",
    )
    requested_at: datetime = Field(..., description="When the command was issued.")


class EnumDelegatedFixOutcome(StrEnum):
    """Terminal outcome of a single delegated-fix attempt."""

    ACCEPTED = "accepted"
    NO_CHANGES = "no_changes"
    GATE_FAILED = "gate_failed"
    REFUSED_SIZE_GATE = "refused_size_gate"
    REFUSED_DENYLIST = "refused_denylist"
    REFUSED_NOT_A_WORKTREE = "refused_not_a_worktree"
    ERROR = "error"


class ModelDelegatedFixResult(BaseModel):
    """Result of a delegated fix attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    repo: str = Field(...)
    pr_number: int = Field(...)
    outcome: EnumDelegatedFixOutcome = Field(...)
    delegation_model: str = Field(
        ..., description="Tool/model identity, e.g. 'ruff-deterministic'."
    )
    cost_usd: float = Field(default=0.0, ge=0.0)
    worktree_path: str | None = Field(default=None)
    commit_sha: str | None = Field(
        default=None, description="Commit SHA when a fix was committed, else None."
    )
    files_changed: int = Field(default=0, ge=0)
    lines_changed: int = Field(default=0, ge=0)
    detail: str = Field(..., description="Human-readable outcome explanation.")
    error: str | None = Field(default=None)
    completed_at: datetime = Field(...)

    @property
    def is_success(self) -> bool:
        return self.outcome == EnumDelegatedFixOutcome.ACCEPTED


__all__: list[str] = [
    "EnumDelegatedFixOutcome",
    "ModelDelegatedFixCommand",
    "ModelDelegatedFixResult",
]
