# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed task, dispatch authority and receipt of one RSD migration (OMN-21373)."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.model_delegated_code_edit import (
    ModelCodeEditResult,
)

_NODE_NAME = re.compile(r"^node_[a-z][a-z0-9_]*$")


class ModelRsdDispatchAuthority(BaseModel):
    """The task-class facts a migration dispatch runs under, read from the registry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str
    source: str = Field(description="The entry point the authority was loaded from.")
    gateway_exposure: str
    timeout_ceiling_seconds: int | None = None


class ModelRsdMigrationTask(BaseModel):
    """One script to move into one omnimarket node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_label: str = Field(
        ..., min_length=1, description="Where the script came from, e.g. repo:path@sha."
    )
    source_text: str = Field(..., min_length=1, max_length=12_000)
    node_name: str
    worktree: str
    purpose: str = Field(..., min_length=1, max_length=2_000)
    max_turns: int = Field(default=20, ge=1, le=40)
    caller_lane: str | None = None
    ticket: str | None = Field(default=None, pattern=r"^OMN-[0-9]+$")

    @field_validator("node_name")
    @classmethod
    def _node_name(cls, value: str) -> str:
        if not _NODE_NAME.match(value):
            raise ValueError(f"{value!r} is not a node package name (node_<name>)")
        return value

    @field_validator("worktree")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError(f"worktree must be absolute: {value!r}")
        return value


class ModelRsdMigrationReceipt(BaseModel):
    """What one RSD migration run did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: str
    node_name: str
    source_label: str
    authority: ModelRsdDispatchAuthority | None = None
    refusal: str = ""
    code_edit: ModelCodeEditResult | None = None
    entry_point_registered: bool = False
    shape_problems: tuple[str, ...] = ()


__all__ = [
    "ModelRsdDispatchAuthority",
    "ModelRsdMigrationReceipt",
    "ModelRsdMigrationTask",
]
