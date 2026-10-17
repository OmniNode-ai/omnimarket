# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed deployment, request, part event and result of the lab history archive (OMN-17427)."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RETENTION_FLOOR_DAYS = 7


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelArchiveSource(_Frozen):
    """A declared root. include and exclude match the path relative to the root."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    root: Path
    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()

    @field_validator("root")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        value = value.expanduser()
        if not value.is_absolute():
            raise ValueError("a source root must be an absolute path")
        return value


class ModelArchiveBudget(_Frozen):
    """What the run may take from the host: CPU, IO, bandwidth, parts and time."""

    bytes_per_second: int = Field(default=4_000_000, ge=1)
    nice: int = Field(default=19, ge=0, le=19)
    io_class: Literal["idle", "best-effort"] = "idle"
    part_max_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    max_parts_per_run: int = Field(default=24, ge=1)
    max_run_seconds: int = Field(default=3 * 3600, ge=1)


class ModelArchiveDrive(_Frozen):
    """The Drive destination: a configured uploader remote and a folder under it."""

    remote: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    folder: str = Field(min_length=1)


class ModelArchiveHost(_Frozen):
    """One lab host: the names it answers to and the roles it carries."""

    id: str = Field(min_length=1)
    hostnames: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()


class ModelArchiveSchedule(_Frozen):
    """The nightly tick: an off-peak window in a named timezone, once per interval."""

    timezone: str = "America/New_York"
    window_start: dt.time = dt.time(1, 0)
    window_end: dt.time = dt.time(5, 0)
    run_interval_seconds: int = Field(default=86400, ge=3600)
    dry_run: bool = False


class ModelLabHistoryArchiveDeployment(_Frozen):
    """Everything specific to one deployment: hosts and roles, sources, staging, Drive."""

    hosts: tuple[ModelArchiveHost, ...]
    refuse_roles: tuple[str, ...] = ("landing_controller",)
    sources: tuple[ModelArchiveSource, ...] = Field(min_length=1)
    staging_dir: Path
    drive: ModelArchiveDrive
    protect: tuple[str, ...] = ()
    budget: ModelArchiveBudget = ModelArchiveBudget()
    schedule: ModelArchiveSchedule = ModelArchiveSchedule()

    @field_validator("staging_dir")
    @classmethod
    def _expand_staging(cls, value: Path) -> Path:
        value = value.expanduser()
        if not value.is_absolute():
            raise ValueError("staging_dir must be an absolute path")
        return value


class ModelLabHistoryArchiveRequest(_Frozen):
    """One archive pass on this host. retention_days never goes below the floor."""

    retention_days: int = Field(default=RETENTION_FLOOR_DAYS, ge=RETENTION_FLOOR_DAYS)
    as_of: dt.datetime | None = None
    dry_run: bool = False
    host_name: str | None = None
    overlay_path: Path | None = None


class EnumLabHistoryArchiveVerdict(StrEnum):
    ARCHIVED = "ARCHIVED"
    NOTHING_TO_ARCHIVE = "NOTHING_TO_ARCHIVE"
    DRY_RUN = "DRY_RUN"
    REFUSED = "REFUSED"
    FAILED = "FAILED"


class EnumLabHistoryArchiveRefusal(StrEnum):
    OVERLAY_INVALID = "OVERLAY_INVALID"
    HOST_ROLE_UNKNOWN = "HOST_ROLE_UNKNOWN"
    HOST_IS_LANDING_CONTROLLER = "HOST_IS_LANDING_CONTROLLER"
    STAGING_INSIDE_SOURCE = "STAGING_INSIDE_SOURCE"
    STAGING_ON_SOURCE_DEVICE = "STAGING_ON_SOURCE_DEVICE"
    UPLOADER_UNAVAILABLE = "UPLOADER_UNAVAILABLE"
    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
    ALREADY_RUNNING = "ALREADY_RUNNING"


class EnumLabHistoryArchivePartStatus(StrEnum):
    DRY_RUN = "DRY_RUN"
    ARCHIVED = "ARCHIVED"
    STAGE_FAILED = "STAGE_FAILED"
    UPLOAD_FAILED = "UPLOAD_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"


class ModelLabHistoryArchivePartEvent(_Frozen):
    """One part, published as it settles: what was staged, where it went, what was freed."""

    host: str
    source: str
    part: str
    status: EnumLabHistoryArchivePartStatus
    files: int
    bytes_source: int
    bytes_staged: int = 0
    drive_path: str = ""
    md5: str = ""
    files_deleted: int = 0
    files_kept_changed: int = 0
    bytes_freed: int = 0
    detail: str = ""


class ModelLabHistoryArchiveResult(_Frozen):
    """The run: a verdict, a refusal code when refused, and every part event."""

    verdict: EnumLabHistoryArchiveVerdict
    host: str = ""
    cutoff: dt.datetime | None = None
    refusal: EnumLabHistoryArchiveRefusal | None = None
    detail: str = ""
    parts: tuple[ModelLabHistoryArchivePartEvent, ...] = ()

    @property
    def bytes_archived(self) -> int:
        return sum(
            p.bytes_staged
            for p in self.parts
            if p.status == EnumLabHistoryArchivePartStatus.ARCHIVED
        )

    @property
    def bytes_freed(self) -> int:
        return sum(p.bytes_freed for p in self.parts)

    def line(self) -> str:
        cells = [
            f"verdict={self.verdict.value}",
            f"host={self.host or '-'}",
            f"parts={len(self.parts)}",
            f"archived_bytes={self.bytes_archived}",
            f"freed_bytes={self.bytes_freed}",
        ]
        if self.refusal is not None:
            cells.append(f"refusal={self.refusal.value}")
        return " ".join(cells)
