# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane worktree retention: the contract's bounds, one run's request and its evidence."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CTX_KEEP_DAYS_FLOOR = 7


class ModelLaneWorktreePolicy(BaseModel):
    """``retention_policy.lane_worktree_retention`` in the node contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs_dir: str
    idle_days: int = Field(ge=1)
    ctx_keep_days: int = Field(ge=CTX_KEEP_DAYS_FLOOR)


class ModelLaneWorktreeRetentionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runs_dir: Path
    now: dt.datetime
    idle_days: int = Field(default=3, ge=1)
    ctx_keep_days: int = Field(default=CTX_KEEP_DAYS_FLOOR, ge=CTX_KEEP_DAYS_FLOOR)
    dry_run: bool = False


class ModelLaneWorktreeRetentionResult(BaseModel):
    """What one pass removed (or, in a dry run, would remove) and why the rest stayed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ran", "dry-run", "skipped", "absent"]
    reason: str = ""
    removed_worktrees: tuple[str, ...] = ()
    removed_ctx: tuple[str, ...] = ()
    kept: dict[str, str] = Field(default_factory=dict)
    freed_bytes: int = 0
    errors: tuple[str, ...] = ()

    def line(self) -> str:
        return (
            f"lane_worktrees={self.status} removed={len(self.removed_worktrees)} "
            f"ctx_removed={len(self.removed_ctx)} kept={len(self.kept)} "
            f"freed={self.freed_bytes / 1e9:.1f}GB errors={len(self.errors)}"
        )
