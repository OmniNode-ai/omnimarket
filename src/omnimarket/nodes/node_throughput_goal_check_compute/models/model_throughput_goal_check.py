# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the hourly check's goal check (OMN-23734).

The request carries what the retired throughput_tick.py goal_tick_fold read: the goal scope (or why it
could not be read), the PR watcher's state, the tick's own finding lines, the previous check's record,
the landing controller's admission rows and the newest controller receipt's per-PR reasons. The result
carries the ``GOAL`` line, the findings, the record the caller keeps and the cells of the check.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_throughput_goal_scope import (
    IN_SCOPE,
    PAUSED,
    UNLISTED,
    ModelGoalScope,
)

_TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class ModelGoalFinding(BaseModel):
    """One finding of the goal check: level, loop, why, fix (the tick's finding tuple)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: str
    loop: str
    why: str
    fix: str = ""


class ModelThroughputGoalCheckRequest(BaseModel):
    """What the tick read for the goal check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: str = Field(
        pattern=_TS, description="ISO-Z time of the tick (the caller's clock)."
    )
    scope: ModelGoalScope | None = Field(
        default=None,
        description="The declared goal scope; None when it could not be read.",
    )
    scope_unread: str = Field(
        default="",
        description="Why the scope could not be read; with no scope, nothing is in scope.",
    )
    watcher_state: dict[str, Any] | None = None
    lines: list[str] = Field(
        default_factory=list, description="The tick's own finding lines so far."
    )
    previous: dict[str, Any] | None = Field(
        default=None, description="The record the last non-read-only tick kept."
    )
    admissions: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="The controller's newest admission row per pull request.",
    )
    admissions_unread: str = Field(
        default="",
        description="Why the admission record could not be read; the lists alone decide then.",
    )
    pr_reasons: dict[str, str] | None = Field(
        default=None,
        description="The newest controller receipt's reasons (repo#n -> reason); None: unread.",
    )
    merge_window_min: int = 60
    green_wait_min: int = 60
    wait_threshold: int = 10
    rate_per_hour: int = 10
    open_fall_hours: int = 6


class ModelThroughputGoalCheckResult(BaseModel):
    """The check's answer. ``record`` is None when the check could not run (nothing to keep)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_line: str
    findings: list[ModelGoalFinding] = Field(default_factory=list)
    lines: list[str] = Field(default_factory=list)
    record: dict[str, Any] | None = None
    changed: bool = False
    cells: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "IN_SCOPE",
    "PAUSED",
    "UNLISTED",
    "ModelGoalFinding",
    "ModelGoalScope",
    "ModelThroughputGoalCheckRequest",
    "ModelThroughputGoalCheckResult",
]
