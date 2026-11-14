# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the hourly check's record step (OMN-23734)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class ModelHeartbeatRow(BaseModel):
    """The stamp and pid the checker reads as the tick's heartbeat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stamp: str = Field(pattern=_TS)
    pid: int


class ModelOpenCountRow(BaseModel):
    """This run's open count and the points earlier runs kept on the same basis (oldest first)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    history: list[dict[str, Any]] = Field(default_factory=list)
    at: str = Field(pattern=_TS)
    count: int
    basis: str


class ModelRepoStatusRow(BaseModel):
    """This run's repo-status verdicts; a repository missing from them closes its unit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: list[dict[str, Any]] = Field(default_factory=list)
    at: str = Field(pattern=_TS)

    @field_validator("verdicts")
    @classmethod
    def _typed(cls, verdicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for v in verdicts:
            if not all(
                isinstance(v.get(k), str) for k in ("repo", "opened_at", "verdict")
            ):
                raise ValueError(
                    "a verdict names its repo, opened_at and verdict as strings"
                )
        return verdicts


class ModelThroughputTickRecordRequest(BaseModel):
    """Which rows to keep this tick. A row left out is not touched."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state_dir: str
    heartbeat: ModelHeartbeatRow | None = None
    open_count: ModelOpenCountRow | None = None
    repo_status: ModelRepoStatusRow | None = None
    goal_record: dict[str, Any] | None = Field(
        default=None,
        description="The record the goal check returned; None keeps nothing.",
    )


class ModelRecordFinding(BaseModel):
    """A row that could not be kept, as the tick's finding (level, loop, why, fix)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: str = "unknown"
    loop: str
    why: str
    fix: str


class ModelThroughputTickRecordResult(BaseModel):
    """What was kept and what could not be. A write failure never raises: it is a finding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    heartbeat_write_error: str | None = None
    written: list[str] = Field(default_factory=list)
    findings: list[ModelRecordFinding] = Field(default_factory=list)
    ok: bool = True


__all__ = [
    "ModelHeartbeatRow",
    "ModelOpenCountRow",
    "ModelRecordFinding",
    "ModelRepoStatusRow",
    "ModelThroughputTickRecordRequest",
    "ModelThroughputTickRecordResult",
]
