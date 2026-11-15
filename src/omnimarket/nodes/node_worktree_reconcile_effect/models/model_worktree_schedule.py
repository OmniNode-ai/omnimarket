# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node owns cadence; the deployment overlay owns hosts and filesystem roots."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    launchd_label: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    interval_seconds: int = Field(gt=0)
    timeout_seconds: int = Field(gt=0)
    on_boot_seconds: int = Field(gt=0)


class ModelWorktreeHostPaths(_Frozen):
    """The four paths of one host whose layout differs from the shared overlay ones."""

    python: str | None = Field(default=None, min_length=1)
    project: str | None = Field(default=None, min_length=1)
    state_directory: str | None = Field(default=None, min_length=1)
    environment_file: str | None = Field(default=None, min_length=1)


class ModelWorktreeScheduleOverlay(_Frozen):
    python: str = Field(min_length=1)
    project: str = Field(min_length=1)
    state_directory: str = Field(min_length=1)
    environment_file: str = Field(min_length=1)
    # Correlation IDs in these templates are replaced with a fresh UUID each tick.
    hosts: dict[str, ModelWorktreeReconcileCommand] = Field(min_length=1)
    # Host to its launch manager; a host the mapping does not list runs under systemd (OMN-22799).
    schedulers: dict[str, Literal["systemd", "launchd"]] = Field(default_factory=dict)
    # Host to its own paths; a host the mapping does not list uses the globals (OMN-22799).
    host_paths: dict[str, ModelWorktreeHostPaths] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _schedulers_name_declared_hosts(self) -> Self:
        undeclared = sorted(set(self.schedulers) - set(self.hosts))
        if undeclared:
            raise ValueError(
                "schedulers names hosts the overlay does not declare: "
                + ", ".join(undeclared)
            )
        return self

    @model_validator(mode="after")
    def _host_paths_name_declared_hosts(self) -> Self:
        undeclared = sorted(set(self.host_paths) - set(self.hosts))
        if undeclared:
            raise ValueError(
                "host_paths names hosts the overlay does not declare: "
                + ", ".join(undeclared)
            )
        return self

    def for_host(self, host: str) -> Self:
        """The overlay with this host's path overrides applied; globals where none is given."""
        paths = self.host_paths.get(host)
        updates: dict[str, str] = {}
        if paths is not None:
            for field in ("python", "project", "state_directory", "environment_file"):
                value = getattr(paths, field)
                if value is not None:
                    updates[field] = value
        return self.model_copy(update=updates)


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
    plist: str = ""
    revision: str = ""
