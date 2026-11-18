# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host disk reading event and its typed failure (OMN-23865).

One ``ModelHostDiskRead`` per host run, on the overlay's read topic: free and total bytes per declared volume and the
consumers by class. One ``ModelHostDiskReadFailed`` on the overlay's failed topic when the run could not publish a sound
reading. node_projection_host_disk keeps the newest reading per host and class; OMN-23866's compute reads that
projection and nothing else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
DiskClassName = Literal[
    "uv_cache",
    "docker_images",
    "docker_build_cache",
    "docker_volumes",
    "docker_exited_containers",
    "worktrees",
    "lab_run",
]
FILESYSTEM_CLASSES: tuple[DiskClassName, ...] = ("uv_cache", "worktrees", "lab_run")
DOCKER_CLASSES: tuple[DiskClassName, ...] = (
    "docker_images",
    "docker_build_cache",
    "docker_volumes",
    "docker_exited_containers",
)
ALL_CLASSES: tuple[DiskClassName, ...] = FILESYSTEM_CLASSES + DOCKER_CLASSES
FailureReason = Literal[
    "volume_unreadable", "class_sizes_exceed_volume", "publish_refused"
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelDiskGroup(_Frozen):
    """The entries of one class that share a package and a revision (a copy count above one is the waste).

    For worktrees and lab-run folders the entry name is the ``package`` and ``revision`` is empty.
    """

    package: str
    revision: str
    copies: int = Field(ge=1)
    bytes: int = Field(ge=0)
    #: None where the class has no last-use time to read (docker reports sizes only).
    newest_use_age_seconds: int | None = Field(ge=0, default=None)
    oldest_use_age_seconds: int | None = Field(ge=0, default=None)
    held_copies: int = Field(ge=0)
    owners: tuple[str, ...] = ()


class ModelDiskClassReading(_Frozen):
    """One class on one host: read (with its groups), or unread with the reason, never a guessed size."""

    class_name: DiskClassName
    status: Literal["read", "unread"]
    unread_reason: str = ""
    total_bytes: int = Field(ge=0, default=0)
    entry_count: int = Field(ge=0, default=0)
    revision_count: int = Field(ge=0, default=0)
    #: Largest first, at most the overlay's ``max_groups_per_class``; the totals above cover every entry.
    groups: tuple[ModelDiskGroup, ...] = ()
    groups_omitted: int = Field(ge=0, default=0)
    #: Same-user processes that refused a read: an entry one of them holds may not be marked held.
    unreadable_process_count: int = Field(ge=0, default=0)


class ModelHostDiskVolume(_Frozen):
    path: NonEmptyStr
    total_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)


class ModelHostDiskRead(_Frozen):
    """Published on the read topic, one per host run."""

    correlation_id: NonEmptyStr
    host_name: NonEmptyStr
    read_at: datetime
    volumes: tuple[ModelHostDiskVolume, ...] = Field(min_length=1)
    classes: tuple[ModelDiskClassReading, ...] = Field(min_length=1)


class ModelHostDiskReadFailed(_Frozen):
    """Published on the failed topic; a failed run publishes no read event."""

    correlation_id: NonEmptyStr
    host_name: NonEmptyStr
    read_at: datetime
    reason: FailureReason
    detail: NonEmptyStr


__all__: list[str] = [
    "ALL_CLASSES",
    "DOCKER_CLASSES",
    "FILESYSTEM_CLASSES",
    "DiskClassName",
    "FailureReason",
    "ModelDiskClassReading",
    "ModelDiskGroup",
    "ModelHostDiskRead",
    "ModelHostDiskReadFailed",
    "ModelHostDiskVolume",
]
