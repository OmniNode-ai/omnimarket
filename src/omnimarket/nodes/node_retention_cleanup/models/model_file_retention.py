# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed file retention policy, requests and results."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MIN_KEEP_DAYS = 7


class ModelFileRetentionEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    path: str
    pattern: str = "*"
    keep_days: int = Field(ge=MIN_KEEP_DAYS)
    action: Literal["delete", "archive"]
    exclude: tuple[str, ...] = Field(default_factory=tuple)
    readers: tuple[str, ...] = Field(default_factory=tuple)
    enabled: bool = True
    consent_ref: str = ""
    safety: Literal["files", "lab_lane"] = "files"
    archive_subdir: str = ""


class ModelFileRetentionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    archive_root_env: str
    synced_marker_xattr: str = ""
    entries: list[ModelFileRetentionEntry]


class ModelFileRetentionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[ModelFileRetentionEntry]
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


class ModelFileRetentionItemResult(BaseModel):
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
    bytes_pending_sync: int = 0
    error: str = ""
    note: str = ""
    kept: dict[str, str] = Field(default_factory=dict)


class ModelFileRetentionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    dry_run: bool
    items: list[ModelFileRetentionItemResult] = Field(default_factory=list)
    files_eligible: int = 0
    files_verified: int = 0
    files_removed: int = 0
    bytes_removed: int = 0
    files_archived: int = 0
    bytes_archived: int = 0
