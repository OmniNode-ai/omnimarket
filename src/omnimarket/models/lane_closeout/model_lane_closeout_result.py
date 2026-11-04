# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result of the guard, WIP push and closeout of one tree."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.models.lane_closeout.enum_lane_guard_verdict import (
    EnumLaneGuardVerdict,
)


class ModelLaneCloseoutResult(BaseModel):
    """The guard verdict, the WIP branch pushed (if any) and the TERMINAL cell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    tree: str
    verdict: EnumLaneGuardVerdict
    detail: str  # the WIP branch for wip, the reason for keep, empty for ok
    wip_push: str  # the WIP branch pushed, empty when none
    removed: bool
    cell: str  # worktree=removed:<tree> or worktree=kept:<tree>:<reason>
