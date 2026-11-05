# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed requests, policy and results for contract-declared file retention."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MIN_KEEP_DAYS = 7


class FileRetentionEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    path: str
    pattern: str = "*"
    keep_days: int
    action: Literal["delete", "archive"]
    exclude: tuple[str, ...] = Field(default_factory=tuple)
    readers: tuple[str, ...] = Field(default_factory=tuple)
    reference_paths: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Reader file globs: keep units whose file names they still cite",
    )
    enabled: bool = True
    consent_ref: str = ""

    @field_validator("keep_days")
    @classmethod
    def _floor(cls, value: int) -> int:
        if value < MIN_KEEP_DAYS:
            msg = f"keep_days {value} is under the {MIN_KEEP_DAYS}-day floor"
            raise ValueError(msg)
        return value


class FileRetentionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    archive_root_env: str
    synced_marker_xattr: str = ""
    entries: list[FileRetentionEntry]


class FileRetentionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[FileRetentionEntry]
    now: dt.datetime
    dry_run: bool = False
    host: str = ""
    archive_root: str = ""
    synced_marker: str = Field(
        default="",
        description=(
            "Extended attribute the archive tree's sync client sets on a file once "
            "it has registered it; an archive copy without it keeps its local unit"
        ),
    )
    only: tuple[str, ...] = Field(
        default_factory=tuple, description="Entry names to run; empty runs all"
    )


class FileRetentionItemResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: Literal[
        "ok", "dry_run", "absent", "disabled", "archive_unavailable", "error"
    ]
    action: str = ""
    files_eligible: int = 0
    bytes_eligible: int = 0
    files_verified: int = 0
    units_removed: int = 0
    files_removed: int = 0
    bytes_removed: int = 0
    files_archived: int = 0
    bytes_archived: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    units_pending_sync: int = 0
    units_referenced: int = 0
    bytes_pending_sync: int = 0
    error: str = ""
    note: str = ""


class FileRetentionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    dry_run: bool
    items: list[FileRetentionItemResult] = Field(default_factory=list)
    files_eligible: int = 0
    files_verified: int = 0
    files_removed: int = 0
    bytes_removed: int = 0
    files_archived: int = 0
    bytes_archived: int = 0
