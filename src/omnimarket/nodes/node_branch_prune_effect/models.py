# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the remote-branch prune node (OMN-21890)."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumPullRequestState(StrEnum):
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class EnumBranchAction(StrEnum):
    KEEP = "keep"
    WOULD_DELETE = "would_delete"
    DELETED = "deleted"
    RECORD_FAILED = "record_failed"
    DELETE_FAILED = "delete_failed"


class EnumBranchPruneVerdict(StrEnum):
    PRUNED = "pruned"
    DRY_RUN = "dry_run"
    NOTHING_TO_PRUNE = "nothing_to_prune"
    REFUSED = "refused"
    FAILED = "failed"


class ModelBranchPrunePolicy(BaseModel):
    """The keep policy. It is a deployment fact: it comes from the node overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repositories: tuple[str, ...] = ()
    protected_names: tuple[str, ...] = ()
    protected_patterns: tuple[str, ...] = ()
    min_age_days: int = Field(default=0, ge=0)
    # A schedule trigger prunes for real only when the overlay says so.
    scheduled_dry_run: bool = True


class ModelBranchPruneRequest(BaseModel):
    """One prune run. Dry run is the default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dry_run: bool = True
    as_of: dt.datetime | None = None


class ModelBranchInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    tip_sha: str
    tip_committed_at: dt.datetime


class ModelPullRequestInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: int
    head_ref: str
    head_sha: str
    state: EnumPullRequestState


class ModelBranchDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    repository: str
    branch: str
    tip_sha: str
    action: EnumBranchAction
    reason: str


class ModelBranchPruneResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: EnumBranchPruneVerdict
    dry_run: bool
    decisions: list[ModelBranchDecision] = Field(default_factory=list)
    detail: str = ""

    @property
    def candidates(self) -> list[ModelBranchDecision]:
        return [d for d in self.decisions if d.action != EnumBranchAction.KEEP]
