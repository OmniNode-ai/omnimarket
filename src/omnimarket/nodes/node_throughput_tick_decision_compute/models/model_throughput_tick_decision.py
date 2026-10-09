# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the merge-throughput tick decisions (OMN-20686)."""

from __future__ import annotations

from typing import Literal

from omnibase_core.types import JsonType
from pydantic import BaseModel, ConfigDict, Field

_TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class ModelThroughputTickRequest(BaseModel):
    """Facts the caller read from the landing controller's files, launchd and the PR watcher.

    The handler reads nothing and has no clock; every field is a fact the caller observed.
    A fact the caller could not read is `None` or an error string, never a guess.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: str = Field(pattern=_TS)
    controller_max_minutes: int = Field(default=15, ge=0)
    controller_run_max_minutes: int = Field(default=30, ge=0)
    watcher_max_minutes: int = Field(default=15, ge=0)
    min_merges_per_hour: int = Field(default=5, ge=0)

    launchctl: Literal["loaded", "not_loaded", "unavailable"]
    launchctl_error: str | None = None
    launchctl_stdout: str = ""
    pid_etime: str | None = Field(
        default=None,
        description="Stripped `ps -o etime=` output for the launchd PID, None when ps failed.",
    )
    ticks: list[dict[str, JsonType]] = Field(
        default_factory=list,
        description="The newest complete controller tick lines, newest first, at most two.",
    )
    ticks_error: str | None = Field(
        default=None,
        description="Why ticks.jsonl could not be read, when it could not.",
    )
    heartbeat_text: str | None = None
    state_json: JsonType | None = Field(
        default=None,
        description="The controller's state.json, parsed; None when unreadable.",
    )

    watcher_path: str | None = None
    watcher_state: JsonType | None = None
    watcher_read_error: str | None = None

    floors_per_repo: dict[str, float] | None = None
    floors_error: str | None = None
    policy_load_error: str | None = None
    policy_error: str = ""
    land_like_operator: list[str] = Field(default_factory=list)


class ModelThroughputTickResult(BaseModel):
    """The tick's finding lines and its status line, exactly as the retired script printed them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: list[str]
    status_line: str
    missing: list[str]
    unknown: list[str]
    checked: list[str]
    exit_code: int = Field(ge=0, le=1)
