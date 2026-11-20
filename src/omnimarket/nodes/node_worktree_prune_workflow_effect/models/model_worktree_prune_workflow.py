# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed plan, host result, report row and run report of the worktree prune workflow."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelPruneHostTarget(BaseModel):
    """One host of a run and the disk bar, in percent used, past which it raises an alarm."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    disk_bar_percent: float = Field(gt=0, le=100, allow_inf_nan=False)


class ModelWorktreePrunePlan(BaseModel):
    """What one run covers: its id, what started it and the hosts in report order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    trigger: Literal["schedule", "ticket_closed", "manual"]
    ticket: str | None = None
    hosts: tuple[ModelPruneHostTarget, ...] = Field(min_length=1)
    max_attempts: int = Field(default=3, ge=1, le=10)

    @field_validator("hosts")
    @classmethod
    def _unique_hosts(
        cls, hosts: tuple[ModelPruneHostTarget, ...]
    ) -> tuple[ModelPruneHostTarget, ...]:
        names = [target.host for target in hosts]
        if len(names) != len(set(names)):
            raise ValueError(f"a host is named twice in the plan: {names}")
        return hosts


class ModelPrunedTree(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    freed_bytes: int = Field(ge=0)


class ModelKeptTree(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ModelHostPruneResult(BaseModel):
    """What one host activity returns: the disk readings, the trees removed and the trees kept."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    disk_used_percent_before: float = Field(ge=0, le=100, allow_inf_nan=False)
    disk_used_percent_after: float = Field(ge=0, le=100, allow_inf_nan=False)
    removed: tuple[ModelPrunedTree, ...] = ()
    kept: tuple[ModelKeptTree, ...] = ()

    @property
    def freed_bytes(self) -> int:
        return sum(tree.freed_bytes for tree in self.removed)


class ModelDiskAlarm(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    used_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    bar_percent: float = Field(gt=0, le=100, allow_inf_nan=False)


class ModelHostPruneReportRow(BaseModel):
    """One host's row: ok with its result, or failed with the last error after every attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    status: Literal["ok", "failed"]
    attempts: int = Field(ge=1)
    result: ModelHostPruneResult | None = None
    error: str | None = None
    alarm: ModelDiskAlarm | None = None


class ModelWorktreePruneRunReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    trigger: Literal["schedule", "ticket_closed", "manual"]
    rows: tuple[ModelHostPruneReportRow, ...]

    @property
    def alarms(self) -> tuple[ModelDiskAlarm, ...]:
        return tuple(row.alarm for row in self.rows if row.alarm is not None)

    @property
    def freed_bytes(self) -> int:
        return sum(row.result.freed_bytes for row in self.rows if row.result)


__all__ = [
    "ModelDiskAlarm",
    "ModelHostPruneReportRow",
    "ModelHostPruneResult",
    "ModelKeptTree",
    "ModelPruneHostTarget",
    "ModelPrunedTree",
    "ModelWorktreePrunePlan",
    "ModelWorktreePruneRunReport",
]
