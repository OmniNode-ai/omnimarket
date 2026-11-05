# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared policy of the temp-directory retention node, and the report of one pass.

Every host, path and number the node acts on is a field of the overlay model below. The contract
declares only the bounds an overlay may not exceed (which leaf names a temp directory may have, which
store kinds exist) and the schedule.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelRetentionWindows(_Frozen):
    """The age windows of a run directory: a long one, and a short one once the filesystem is full.

    These are the values the host's RAM-filesystem upkeep declares for its run directories
    (``retain_hours``, ``pressure_percent``, ``pressure_retain_hours``); an entry under a temp
    directory is held to the same windows.
    """

    retain_hours: float = Field(gt=0)
    pressure_percent: int = Field(gt=0, le=100)
    pressure_retain_hours: float = Field(gt=0)

    @model_validator(mode="after")
    def pressure_is_shorter(self) -> Self:
        if self.pressure_retain_hours > self.retain_hours:
            raise ValueError("pressure_retain_hours must not exceed retain_hours")
        return self


class ModelTempRoot(_Frozen):
    """One temp directory whose direct entries age out when no live process holds them.

    ``protected_name_patterns`` are full-match regular expressions of entry names that are never
    removed (a live session's own working directory, for one).
    """

    path: NonEmptyStr
    protected_name_patterns: tuple[NonEmptyStr, ...] = ()


class ModelStoreCeiling(_Frozen):
    """The most MiB a tool's package store may hold once its filesystem is under pressure."""

    path: NonEmptyStr
    kind: Literal["pnpm"]
    ceiling_mib: int = Field(gt=0)


class ModelTempRetentionHost(_Frozen):
    """All deployment facts the node needs on one host."""

    name: NonEmptyStr
    scheduler: Literal["systemd-user"]
    windows: ModelRetentionWindows
    temp_roots: Annotated[tuple[ModelTempRoot, ...], Field(min_length=1)]
    store_ceilings: tuple[ModelStoreCeiling, ...] = ()


class ModelTempRetentionOverlay(_Frozen):
    """The hosts that declare temp-directory retention."""

    hosts: Annotated[tuple[ModelTempRetentionHost, ...], Field(min_length=1)]


class ModelRetentionCadence(_Frozen):
    """A first pass followed by a fixed interval between run starts."""

    first_pass: NonEmptyStr
    interval_seconds: int = Field(gt=0)
    accuracy: NonEmptyStr
    timeout_start_seconds: int = Field(gt=0)
    description: NonEmptyStr


class ModelRetentionBounds(_Frozen):
    """What an overlay may not exceed, owned by the contract."""

    temp_root_leaf_names: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    store_leaf_names: dict[Literal["pnpm"], NonEmptyStr] = Field(min_length=1)


class ModelRetentionSchedule(_Frozen):
    """The schedule block owned by the node contract."""

    unit_name: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    ]
    cadence: ModelRetentionCadence


class ModelRetentionPass(_Frozen):
    """What one pass over one host's declared directories did.

    ``removed`` and ``busy`` list entry paths (capped by the caller); the ``*_count`` fields are
    exact. ``window_hours`` is the window each root was held to.
    """

    host: str
    dry_run: bool
    removed: tuple[str, ...] = ()
    removed_count: int = 0
    freed_mib: int = 0
    busy: tuple[str, ...] = ()
    busy_count: int = 0
    kept_count: int = 0
    refused: tuple[str, ...] = ()
    windows: dict[str, float] = Field(default_factory=dict)
    stores: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class ModelRetentionScheduleResult(_Frozen):
    """A schedule verb's process status and output."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
