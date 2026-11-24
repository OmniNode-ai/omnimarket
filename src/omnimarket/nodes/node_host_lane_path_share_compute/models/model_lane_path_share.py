# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the lane path share check (OMN-25931).

A reading is one lane path of one host: what it is, where it is, the mount point of the filesystem it sits on
(``findmnt -T <path>``) and its size in bytes. The request names which mount points are the host's mounted data
drives; nothing here reads a disk.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

BAR_PERCENT = 10.0


class ModelLanePathReading(BaseModel):
    """One lane path: its kind (worktrees, run dir, tmp, cache, codex home, venv, runner work dir), path, mount point and bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    path: str = Field(min_length=1)
    mount_point: str = Field(min_length=1)
    bytes_used: int = Field(ge=0)


class ModelHostLanePaths(BaseModel):
    """A host's lane paths, the mount points that are its mounted data drives, and the lane that owns its switchover."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    data_drive_mounts: tuple[str, ...]
    paths: tuple[ModelLanePathReading, ...]


class ModelLanePathShareRequest(BaseModel):
    """``bar_percent``: a host whose lane path bytes on mounted data drives are under this share alarms."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    hosts: tuple[ModelHostLanePaths, ...]
    bar_percent: float = Field(default=BAR_PERCENT, gt=0, le=100)


class ModelHostShare(BaseModel):
    """One host's reading: bytes on data drives, total bytes, the share, and the verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    owner: str
    data_drive_bytes: int
    total_bytes: int
    share_percent: float
    state: Literal["ok", "alarm", "indeterminate"]
    reason: str = ""


class ModelLanePathShareResult(BaseModel):
    """``rows`` are ledger STATUS rows: an alarm row per host under the bar, an ok row per host at or over it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["success", "refused"]
    exit_code: int = 0
    reason: str = ""
    hosts: tuple[ModelHostShare, ...] = ()
    rows: tuple[str, ...] = ()
