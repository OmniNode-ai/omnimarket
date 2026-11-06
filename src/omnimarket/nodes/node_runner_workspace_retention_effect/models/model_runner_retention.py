# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runner-local lifecycle retention input and measured outcome."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelRunnerRetentionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    work_directory: str
    cache_budgets: dict[str, Annotated[int, Field(ge=0)]]
    protected_directories: tuple[str, ...]


class ModelRunnerRetentionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["retain_runner_workspace"] = "retain_runner_workspace"
    runner_home: str
    workspace: str
    phase: Literal["job_started", "job_completed"]
    policy: ModelRunnerRetentionPolicy | None = None
    lifecycle_boundary: bool = False
    active_workspaces: tuple[str, ...] = ()
    dry_run: bool = True


class ModelRunnerRetentionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["completed", "refused", "locked", "failed"]
    dry_run: bool
    reason: str = ""
    removed: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    planned: tuple[str, ...] = ()
    bytes_before: int | None = None
    bytes_after: int | None = None
    bytes_removed: int | None = None
