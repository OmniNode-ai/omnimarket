# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the merge-sweep effect node (OMN-20676)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.merge_sweep import (
    ModelMergeSweepClaimCheckRequest,
    ModelMergeSweepReadRequest,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

MAX_AGE_S = 480.0
FULL_RESYNC_MAX_AGE_S = 90 * 60.0
TICKS_READ = 20


class ModelMergeSweepLoadRequest(BaseModel):
    """Where the sweep's facts are, and the host's load.

    ``state_path`` is the PR watcher's state file, the only source of PR and check facts: nothing
    is read from GitHub. ``ledger_path`` is the rolling work ledger, ``ticks_path`` the landing
    controller's tick log (absent on a host the controller does not run on) and ``floors_path`` the
    per-repository merge floors. ``load1`` and ``cpus`` default to this host's own reading.
    """

    model_config = _FROZEN

    state_path: str
    ledger_path: str
    ticks_path: str | None = None
    floors_path: str
    now: str
    load1: float | None = None
    cpus: int | None = None
    window_min: int = 60
    max_reds: int = 40
    max_age_s: float = Field(default=MAX_AGE_S, gt=0)
    claim_check_pr: str | None = None


class ModelMergeSweepLoadResult(BaseModel):
    """The facts of one reading, or why the state could not be used. Never a partial reading."""

    model_config = _FROZEN

    ok: bool
    why: str = ""
    facts: ModelMergeSweepReadRequest | None = None
    claim_check: ModelMergeSweepClaimCheckRequest | None = None


class ModelMergeSweepLaneRunRequest(BaseModel):
    """One start of one lane through the remote-lane runner, and the wait for its receipt."""

    model_config = _FROZEN

    runner_script: str
    brief_path: str
    brief_text: str
    lane: str
    model: str
    parent: str
    ticket: str
    prs: list[str] = Field(default_factory=list)
    repo: str | None = None
    host: str | None = None
    start_timeout_s: float = Field(default=120.0, gt=0)
    wait_timeout_s: float = Field(default=600.0, gt=0)
    max_waits: int = Field(default=40, ge=1)


class ModelMergeSweepLaneRunResult(BaseModel):
    """The lane's final receipt fields, or ``start-failed`` with the runner's output."""

    model_config = _FROZEN

    started: bool
    argv: list[str]
    exit_code: int
    status: str
    host: str | None = None
    duration_s: float | None = None
    result: str = ""
    receipt: str = ""
    waits: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)
