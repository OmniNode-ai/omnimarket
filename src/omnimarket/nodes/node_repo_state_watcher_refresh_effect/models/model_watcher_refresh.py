# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O and overlay of node_repo_state_watcher_refresh_effect (OMN-23033).

The refresh is a command a host declares for the PR watcher mirror: it runs the steps the
host's overlay names (ask the PR watcher for a tick; bring the tick's state to this host when
the watcher runs elsewhere), then reads the host's watcher state again. It answers
``refreshed`` only when the state's read time moved forward, so a command that changed
nothing never reads as current.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

OVERLAY_ID = "node_repo_state_watcher_refresh_effect"
OVERLAY_FILE_ENV = "ONEX_REPO_STATE_WATCHER_REFRESH_OVERLAY"


class EnumWatcherRefreshStatus(StrEnum):
    REFRESHED = "refreshed"
    UNCHANGED = "unchanged"
    FAILED = "failed"


class ModelWatcherRefreshOverlay(BaseModel):
    """Every setting the refresh command reads; no key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Environment variable naming the PR watcher's state file on this host.
    watcher_state_env: str = Field(min_length=1)
    #: Environment variable holding the command that asks the PR watcher for one tick and waits
    #: for its terminal (shell words; ``{repo}`` is replaced by the repository).
    tick_command_env: str = Field(min_length=1)
    #: Environment variable holding the command that brings the tick's state to this host, or
    #: null on the host that runs the watcher itself. The key is required either way.
    state_sync_command_env: str | None
    step_timeout_seconds: float = Field(gt=0)


class ModelWatcherRefreshRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ModelWatcherRefreshStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    ok: bool
    returncode: int | None
    duration_ms: int = Field(ge=0)
    tail: str = ""


class ModelWatcherRefreshResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    status: EnumWatcherRefreshStatus
    ok: bool
    read_at_before: datetime | None
    read_at_after: datetime | None
    steps: tuple[ModelWatcherRefreshStep, ...]
    message: str = ""


__all__ = [
    "OVERLAY_FILE_ENV",
    "OVERLAY_ID",
    "EnumWatcherRefreshStatus",
    "ModelWatcherRefreshOverlay",
    "ModelWatcherRefreshRequest",
    "ModelWatcherRefreshResult",
    "ModelWatcherRefreshStep",
]
