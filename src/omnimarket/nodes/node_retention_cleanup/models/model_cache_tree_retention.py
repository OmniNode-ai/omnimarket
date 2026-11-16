# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rebuildable cache-tree retention models (OMN-19998).

A cache tree is a directory whose direct children are rebuildable units
(Python virtual environments of the dod-verify check). A unit is removed
whole with ``shutil.rmtree`` — symlinks inside it included and never
followed — only when every regular file inside it is older than the entry's
``keep_days``. The models here are frozen, like the file-retention models
beside them.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_retention_cleanup.models.model_file_retention import (
    MIN_KEEP_DAYS,
)


class CacheTreeEntry(BaseModel):
    """One rebuildable cache tree declared in the retention contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    path: str
    pattern: str = "*"
    keep_days: int
    readers: tuple[str, ...] = Field(min_length=1)
    enabled: bool = True

    @field_validator("keep_days")
    @classmethod
    def _keep_days_floor(cls, value: int) -> int:
        if value < MIN_KEEP_DAYS:
            msg = f"keep_days must be at least {MIN_KEEP_DAYS}"
            raise ValueError(msg)
        return value


class CacheTreePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[CacheTreeEntry]


class CacheTreeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[CacheTreeEntry]
    now: dt.datetime
    dry_run: bool = False
    only: tuple[str, ...] = ()


class CacheTreeItemResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: Literal["ok", "dry_run", "absent", "disabled", "error"]
    units_eligible: int = 0
    units_removed: int = 0
    files_removed: int = 0
    bytes_eligible: int = 0
    bytes_removed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    error: str = ""


class CacheTreeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[CacheTreeItemResult]
    units_removed: int = 0
    files_removed: int = 0
    bytes_removed: int = 0
