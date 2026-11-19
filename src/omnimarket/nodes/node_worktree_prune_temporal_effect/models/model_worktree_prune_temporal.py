# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed plan, report request, host row and run result of the worktree prune workflow."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelPruneHostTarget(BaseModel):
    """One host of a run: its pruner command, the path whose disk use is read and the bar past which it alarms."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    task_queue: str = Field(
        min_length=1, description="The queue that host's worker polls."
    )
    pruner_argv: tuple[str, ...] = Field(
        min_length=1,
        description="The declared pruner node command; it prints the run's result as its last line.",
    )
    disk_path: str = Field(min_length=1)
    disk_bar_percent: float = Field(gt=0, le=100, allow_inf_nan=False)


class ModelWorktreePrunePlan(BaseModel):
    """What one run covers: what started it and the hosts in report order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger: Literal["schedule", "ticket_closed", "manual"] = "manual"
    ticket: str | None = None
    hosts: tuple[ModelPruneHostTarget, ...] = Field(min_length=1)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_initial_seconds: int = Field(default=10, ge=1)
    activity_timeout_seconds: int = Field(default=1800, ge=1)
    report_argv: tuple[str, ...] = Field(
        min_length=1,
        description="This node's own report command (python -m ... report).",
    )


class ModelPruneReportRequest(BaseModel):
    """The report step's input: one host, what its pruner printed (or why it failed) and the step key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_key: str = Field(min_length=1)
    host: str = Field(min_length=1)
    disk_path: str = Field(min_length=1)
    disk_bar_percent: float = Field(gt=0, le=100, allow_inf_nan=False)
    pruner_output: str | None = None
    error: str | None = None


class ModelDiskAlarm(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    used_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    bar_percent: float = Field(gt=0, le=100, allow_inf_nan=False)


class ModelPruneHostRow(BaseModel):
    """One host's row: the pruner's counts and the disk reading, or the failure after every attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_key: str
    host: str
    status: Literal["ok", "failed"]
    scanned: int = 0
    removed: int = 0
    kept: int = 0
    needs_human: int = 0
    failures: int = 0
    freed_bytes: int = 0
    disk_used_percent: float | None = None
    alarm: ModelDiskAlarm | None = None
    error: str | None = None


class ModelWorktreePruneRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    trigger: str
    rows: tuple[ModelPruneHostRow, ...] = ()

    @property
    def alarms(self) -> tuple[ModelDiskAlarm, ...]:
        return tuple(r.alarm for r in self.rows if r.alarm is not None)

    @property
    def freed_bytes(self) -> int:
        return sum(r.freed_bytes for r in self.rows)


__all__ = [
    "ModelDiskAlarm",
    "ModelPruneHostRow",
    "ModelPruneHostTarget",
    "ModelPruneReportRequest",
    "ModelWorktreePrunePlan",
    "ModelWorktreePruneRunResult",
]
