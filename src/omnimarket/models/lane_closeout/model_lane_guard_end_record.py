# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The record the lane guard writes on a kill: read-only facts about the dead lane."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EnumLaneGuardEndCause(StrEnum):
    OOM_KILL = "oom_kill"
    RUNTIME_MAX = "runtime_max"
    SIGNAL = "signal"
    EXIT_CODE = "exit_code"


class ModelLaneGuardEndTree(BaseModel):
    """The guard's read-only snapshot of one of the dead lane's trees."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    branch: str = Field(description="The tree's branch; empty when it is detached.")
    last_pushed_head: str = Field(
        description="The sha of the upstream or origin/<branch> tip; empty when there is none."
    )
    unpushed_commits: int = Field(ge=0)
    changed_files: int = Field(ge=0)
    readable: bool = Field(
        description="False when git could not be read: the counts are then 0 and error says why."
    )
    error: str = ""


class ModelLaneGuardEndRecord(BaseModel):
    """The record the guard writes on a kill: read-only facts about the dead lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    unit: str
    lane: str
    host: str
    mode: str
    cause: EnumLaneGuardEndCause
    exit: int
    signal: int | None
    mem_peak_bytes: int | None
    mem_limit_bytes: int
    mem_enforced: bool = Field(
        description="False on a host whose guard has no memory limit, e.g. macOS."
    )
    runtime_s: float
    runtime_limit_s: int
    ended_at: str
    trees: tuple[ModelLaneGuardEndTree, ...] = Field(default=())
