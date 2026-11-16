# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the sweep did with one tree of an ended lane."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.lane_closeout.enum_lane_guard_verdict import (
    EnumLaneGuardVerdict,
)


class ModelLaneCloseoutTreeOutcome(BaseModel):
    """One tree: its guard verdict, the branch its work was pushed to, and whether it is gone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tree: str
    verdict: EnumLaneGuardVerdict
    pushed_branch: (
        str  # wip/<tag> or <salvage prefix>/<tag>, empty when nothing was pushed
    )
    removed: bool
    cell: str  # worktree=removed:<tree> or worktree=kept:<tree>:<reason>
