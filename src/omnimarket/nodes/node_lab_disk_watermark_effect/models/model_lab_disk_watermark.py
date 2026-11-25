# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay, request and reading of the lab disk watermark (OMN-23384, OMN-23865)."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from omnimarket.models.model_host_disk_reading import (
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


class ModelWatermarkGrowth(_Frozen):
    page_hours_to_floor: float = Field(gt=0)
    min_span_minutes: int = Field(gt=0)


class ModelVolumeGrowth(_Frozen):
    path: str
    free_gb_per_hour: float
    hours_to_floor: float | None
    span_minutes: float


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
    growth: ModelWatermarkGrowth | None = None


class ModelLabDiskWatermarkOverlay(_Frozen):
    hosts: tuple[ModelWatermarkHost, ...] = Field(min_length=1)
    #: Absent: the node only writes the reading file, as before OMN-23865.
    reading: ModelHostDiskReadingSettings | None = None


class ModelLabDiskWatermarkSchedule(_Frozen):
    first_pass: NonEmptyStr
    interval_seconds: int = Field(gt=0)
    accuracy: NonEmptyStr
    timeout_start_seconds: int = Field(gt=0)
    #: The share of ``timeout_start_seconds`` the class scan may spend. The docker reads (two commands, each bounded by the
    #: overlay's ``timeout_seconds``), the inode read and the publish run after it, so the share leaves room for them.
    scan_budget_percent: int = Field(gt=0, lt=100, default=70)
    description: NonEmptyStr


class ModelLabDiskWatermarkRequest(_Frozen):
    """Take one reading for ``host_name``; ``dry_run`` reads and reports and writes no file."""

    host_name: NonEmptyStr
    overlay_file: str = ""
    dry_run: bool = False
    #: Names the host disk event and its failure; a blank one is minted by the handler.
    correlation_id: str = ""
    #: Seconds the class scan may spend before it stops with the classes it has read (OMN-26097); None is unbounded.
    budget_seconds: float | None = Field(default=None, gt=0)


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


class ModelInodeAlarmDeclaration(_Frozen):
    """The inode alarm the contract declares: below which free share it fires, who owns it, and the ledger route it uses.

    The route is the one the lab stack reaper and the deploy agent use: a FRICTION row per alarm and one STATUS row with
    ``alarms=<n>``, appended through the bus append command whose environment variable this declaration names.
    """

    free_percent_below: float = Field(gt=0, lt=100)
    owner: NonEmptyStr
    lane: NonEmptyStr
    ticket: NonEmptyStr
    repeat_minutes: int = Field(gt=0)
    ledger_append_command_env: NonEmptyStr
    ledger_append_timeout_seconds: int = Field(gt=0)


class ModelInodeReading(_Frozen):
    """The inode use of one declared volume; ``free_percent`` is the free inodes over all inodes."""

    path: str
    files: int
    free: int
    free_percent: float
    alarm: bool


class ModelInodeAlarm(_Frozen):
    """One volume read below the declared free share: the rows it writes, whether they were written, and why not."""

    host_name: str
    path: str
    free_percent: float
    free: int
    files: int
    threshold_percent: float
    owner: str
    rows: tuple[str, ...]
    written: bool
    note: str = ""


class ModelClassScanSkip(_Frozen):
    """The entries of one class the scan listed and then found gone (OMN-26097): skipped and counted, never raised."""

    class_name: NonEmptyStr
    skipped_entries: int = Field(ge=1)


class ModelLabDiskWatermarkGrowthReading(ModelLabDiskWatermarkReading):
    """A reading with the growth of each volume against the admission floor (OMN-21663).

    The growth is a result of the run and never part of the ``host-disk-read`` event, so it sits beside the contract's
    output model and leaves that model, which the bus-model seam check pairs with the event's consumer, unchanged.
    """

    growth: tuple[ModelVolumeGrowth, ...] = ()
    growth_paged: bool = False
    #: Inode use per readable volume, the alarms below the declared free share, and the volumes whose inodes were unread
    #: (OMN-24562). Result only, like the growth: the reading file and the host disk event keep their shape.
    inodes: tuple[ModelInodeReading, ...] = ()
    inode_alarms: tuple[ModelInodeAlarm, ...] = ()
    inodes_unread: tuple[str, ...] = ()
    #: Entries a class scan listed and then found gone, per class (OMN-26097). Result only: the event keeps its shape.
    scan_skipped: tuple[ModelClassScanSkip, ...] = ()


class ModelLabDiskWatermarkScheduleResult(_Frozen):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
