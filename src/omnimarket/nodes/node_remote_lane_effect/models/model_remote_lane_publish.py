# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One publish attempt, including recovery when the PR branch advances."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PublishOutcome = Literal[
    "published",
    "already-published",
    "conflict",
    "invalid-history",
    "target-unreadable",
    "replay-failed",
    "push-failed",
    "head-keeps-moving",
    "recovery-push-failed",
]


class ModelRemoteLanePublishRequest(BaseModel):
    """Publish a committed lane delta without replacing a concurrent commit.

    The caller has checked that the PR is open and its head is in the same
    repository. ``base_sha`` is the full commit the lane started from, not the
    moving PR head. All commits in ``base_sha..commit_sha`` are replayed together.
    ``observed_head`` comes from the caller's exact-head read; the effect rereads
    the branch before acting and after a failed pinned push.

    ``replay_root`` is an existing, lane-owned scratch root from the deployment
    overlay. Replay uses a unique linked worktree there; the original tree,
    index, branch and commit remain untouched, including any uncommitted work.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    worktree: str = Field(min_length=1)
    replay_root: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    observed_head: str = Field(pattern=r"^[0-9a-f]{40}$")
    lane: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")
    max_attempts: int = Field(default=3, ge=1, le=5)
    timeout_s: float = Field(default=60.0, gt=0, le=600)


class ModelRemoteLanePublishResult(BaseModel):
    """A published head, or an explicit preservation/refusal result.

    A recovery branch name alone is not evidence of preservation:
    ``recovery_pushed`` records remote acknowledgement. If it is false the
    caller MUST retain the original worktree and commit. Cleanup failures also
    require keeping the named replay tree. Only published/already-published
    outcomes may be handed to landing as a completed target publish.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: PublishOutcome
    message: str
    original_sha: str
    observed_head: str
    published_sha: str | None = None
    recovery_branch: str
    recovery_pushed: bool = False
    keep_worktree: bool = True
    attempts: int = 0
    cleanup_errors: tuple[str, ...] = ()
