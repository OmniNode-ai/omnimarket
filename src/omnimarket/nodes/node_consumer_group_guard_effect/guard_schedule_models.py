# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The guard's contract cadence, deployment overlay and schedule verb result."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelGuardCadence(_Frozen):
    """A first pass followed by a fixed interval between run starts."""

    first_pass: NonEmptyStr
    interval_seconds: int = Field(gt=0)
    accuracy: NonEmptyStr
    description: NonEmptyStr


class ModelGuardSchedule(_Frozen):
    """The schedule block owned by the node contract."""

    unit_name: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    ]
    dispatch_venv: NonEmptyStr
    state_directory: NonEmptyStr
    cadence: ModelGuardCadence
    schedulers: dict[Literal["systemd-user"], NonEmptyStr] = Field(min_length=1)


class ModelGuardHostRow(_Frozen):
    """All deployment facts needed to schedule the guard on one host."""

    name: NonEmptyStr
    scheduler: Literal["systemd-user"]
    bus_lane: NonEmptyStr
    workspace_config_root: NonEmptyStr


class ModelGuardScheduleOverlay(_Frozen):
    """The hosts that declare the guard and their execution environment."""

    hosts: Annotated[tuple[ModelGuardHostRow, ...], Field(min_length=1)]


class ModelGuardScheduleResult(_Frozen):
    """A schedule verb's process status and output."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
