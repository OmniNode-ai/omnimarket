# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol seam for ``run_focused_test_run`` (OMN-19359).

The handler owns the sequence (admit, sweep, prepare, overlay, mutate, run,
tear down) and every decision; the client owns only I/O against the lab host.
Tests drive the handler with a fake client and no ssh, git or docker.

Error seam: a client RAISES :class:`FocusedTestRunInfraError` on any
infrastructure failure (ssh, git, docker, a missing required variable). The
handler turns it into an ``infra_error`` receipt, never a pass, and still
tears down.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class FocusedTestRunInfraError(RuntimeError):
    """An infrastructure fault on the lab host or on the way to it."""


class FocusedTestRunTaskExistsError(FocusedTestRunInfraError):
    """The task directory already exists: a rerun of the same correlation id,
    ref role and attempt. Refused, and the existing directory is left alone."""


class ModelHostAdmission(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    running_task_containers: int = Field(..., ge=0)
    load_one_minute: float = Field(..., ge=0)


class ModelEnvImage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str
    image_id: str


class ModelContainerRunOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int | None
    junit_xml: str | None = Field(
        default=None, description="The junit file's text, or None when absent."
    )
    killed_at_wall_clock: bool = False
    stderr_tail: str = ""


class ModelTeardownOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    container_absent: bool
    worktree_absent: bool


@runtime_checkable
class ProtocolFocusedTestRunClient(Protocol):
    """I/O against the lab host that runs the task container."""

    def host_identity(self) -> str: ...

    def task_root(self) -> str: ...

    def docker_bin(self) -> str: ...

    def admission(self) -> ModelHostAdmission: ...

    def sweep_orphans(self) -> None:
        """Remove labelled containers and task directories older than an hour."""
        ...

    def prepare_worktree(self, repo: str, commit_sha: str, task_dir: str) -> None:
        """Fetch ``commit_sha`` anonymously and add a detached worktree at
        ``task_dir``. RAISE if ``task_dir`` already exists: a rerun with the
        same correlation id is refused, never merged."""
        ...

    def ensure_env_image(self, repo: str, task_dir: str) -> ModelEnvImage:
        """The content-keyed environment image for the worktree's lock."""
        ...

    def read_file(self, task_dir: str, path: str) -> str | None: ...

    def write_files(self, task_dir: str, files: dict[str, str]) -> None: ...

    def remove_paths(self, task_dir: str, paths: tuple[str, ...]) -> None: ...

    def run_container(
        self, argv: list[str], container_name: str, task_dir: str, wall_seconds: int
    ) -> ModelContainerRunOutcome: ...

    def teardown(
        self, repo: str, task_dir: str, correlation_id: str, container_name: str
    ) -> ModelTeardownOutcome: ...


__all__ = [
    "FocusedTestRunInfraError",
    "FocusedTestRunTaskExistsError",
    "ModelContainerRunOutcome",
    "ModelEnvImage",
    "ModelHostAdmission",
    "ModelTeardownOutcome",
    "ProtocolFocusedTestRunClient",
]
