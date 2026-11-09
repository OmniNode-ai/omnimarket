# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay, request and reading of the lab disk watermark (OMN-23384)."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

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


class ModelWatermarkHost(_Frozen):
    name: NonEmptyStr
    scheduler: Literal["systemd-user"]
    reading_file: NonEmptyStr
    volumes: tuple[ModelWatermarkVolume, ...] = Field(min_length=1)


class ModelLabDiskWatermarkOverlay(_Frozen):
    hosts: tuple[ModelWatermarkHost, ...] = Field(min_length=1)


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


class ModelLabDiskWatermarkScheduleResult(_Frozen):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
