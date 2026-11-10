# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node owns cadence; the deployment overlay owns hosts and filesystem roots."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileCommand


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelWorktreeScheduleResources(_Frozen):
    """The unit's priority, written into its drop-in by the installer."""

    nice: int = Field(ge=-20, le=19)
    io_scheduling_class: Literal["best-effort", "idle"]
    io_scheduling_priority: int = Field(ge=0, le=7)
    cpu_weight: int = Field(ge=1, le=10000)


class ModelWorktreeSchedule(_Frozen):
    resources: ModelWorktreeScheduleResources
    unit_name: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$")
    interval_seconds: int = Field(gt=0)
    timeout_seconds: int = Field(gt=0)
    on_boot_seconds: int = Field(gt=0)


class ModelWorktreeScheduleOverlay(_Frozen):
    python: str = Field(min_length=1)
    project: str = Field(min_length=1)
    state_directory: str = Field(min_length=1)
    environment_file: str = Field(min_length=1)
    # Correlation IDs in these templates are replaced with a fresh UUID each tick.
    hosts: dict[str, ModelWorktreeReconcileCommand] = Field(min_length=1)


class ModelWorktreeScheduleRequest(_Frozen):
    mode: Literal["render", "install", "status"] = "render"
    host: str = Field(min_length=1)
    overlay_path: str = Field(min_length=1)
    expected_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class ModelWorktreeScheduleResult(_Frozen):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    service: str = ""
    timer: str = ""
    dropin: str = ""
    revision: str = ""
