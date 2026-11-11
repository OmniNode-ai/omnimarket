# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay, request and reading of the lab disk watermark (OMN-23384, OMN-23865)."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_host_disk_reading import (
    ModelHostDiskRead,
    ModelHostDiskReadFailed,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Level = Literal["ok", "warn", "page"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelWatermarkVolume(_Frozen):
    path: NonEmptyStr
    warn_percent: int = Field(gt=0, le=100)
    page_percent: int = Field(gt=0, le=100)

    @model_validator(mode="after")
    def warn_is_below_page(self) -> Self:
        if self.warn_percent >= self.page_percent:
            raise ValueError("warn_percent must be below page_percent")
        return self


FilesystemClassName = Literal["uv_cache", "worktrees", "lab_run"]


class ModelClassRoot(_Frozen):
    """A directory whose children are one class's entries: the lane package cache, the worktrees, the lab-run folders.

    ``entries_subdir`` is the directory under ``path`` whose children are the entries (empty: the children of ``path``).
    """

    name: FilesystemClassName
    path: NonEmptyStr
    entries_subdir: str = ""


class ModelDockerReader(_Frozen):
    """The two commands that read docker, from the overlay; each prints one JSON object per line.

    ``df_command`` is ``docker system df --format '{{json .}}'`` (images, volumes, build cache) and ``exited_command`` is
    ``docker ps -a --filter status=exited --size --format '{{json .}}'``.
    """

    df_command: tuple[NonEmptyStr, ...] = Field(min_length=1)
    exited_command: tuple[NonEmptyStr, ...] = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)


class ModelHostDiskReadingSettings(_Frozen):
    """How a reading of consumers by class is taken and published: every value the reading needs beyond a host row."""

    tolerance_percent: float = Field(ge=0)
    bus_lane: NonEmptyStr
    read_topic: NonEmptyStr
    failed_topic: NonEmptyStr
    max_groups_per_class: int = Field(gt=0)
    interval_seconds: int = Field(gt=0)


class ModelWatermarkHost(_Frozen):
    name: NonEmptyStr
    scheduler: Literal["systemd-user"]
    reading_file: NonEmptyStr
    #: The free space below which placement refuses a unit on this host (the placement contract's disk bar, or the
    #: host row's own ``min_free_disk_gb``). A page mark that leaves this much free or less would page after the
    #: admission floor has already refused, so the handler refuses such an overlay.
    admission_floor_gb: float = Field(gt=0)
    volumes: tuple[ModelWatermarkVolume, ...] = Field(min_length=1)
    #: The class roots this host declares; a class with no root here is read as an unread class, never from a default.
    class_roots: tuple[ModelClassRoot, ...] = ()
    docker: ModelDockerReader | None = None


class ModelLabDiskWatermarkOverlay(_Frozen):
    hosts: tuple[ModelWatermarkHost, ...] = Field(min_length=1)
    #: Absent: the node only writes the reading file, as before OMN-23865.
    reading: ModelHostDiskReadingSettings | None = None


class ModelLabDiskWatermarkSchedule(_Frozen):
    first_pass: NonEmptyStr
    interval_seconds: int = Field(gt=0)
    accuracy: NonEmptyStr
    timeout_start_seconds: int = Field(gt=0)
    description: NonEmptyStr


class ModelLabDiskWatermarkRequest(_Frozen):
    """Take one reading for ``host_name``; ``dry_run`` reads and reports and writes no file."""

    host_name: NonEmptyStr
    overlay_file: str = ""
    dry_run: bool = False
    #: Names the host disk event and its failure; a blank one is minted by the handler.
    correlation_id: str = ""


class ModelVolumeReading(_Frozen):
    path: str
    total_bytes: int
    free_bytes: int
    used_percent: float
    warn_percent: int
    page_percent: int
    level: Level


class ModelLabDiskWatermarkReading(_Frozen):
    """One reading; ``level`` is the worst of its volumes and ``paged`` is True at ``page``."""

    host_name: str
    read_at: str
    level: Level
    paged: bool
    volumes: tuple[ModelVolumeReading, ...]
    reading_file: str
    written: bool
    #: The host disk event this run published, or the typed failure; None when the overlay declares no ``reading``.
    #: Not part of the reading file: placement reads that file's ``level`` and nothing else changes in it.
    host_disk: ModelHostDiskRead | ModelHostDiskReadFailed | None = None


class ModelLabDiskWatermarkScheduleResult(_Frozen):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
