# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, outcome and result of one release cut tick."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelReleaseCutTickRequest(BaseModel):
    """What a scheduled unit passes: where the registry and the ledger are, and the run's switches."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    registry_root: str = Field(
        min_length=1,
        description="Root that holds the omnimarket, omnibase_infra and omniclaude clones.",
    )
    ledger_path: str = Field(
        min_length=1, description="The rolling work ledger a row is appended to."
    )
    ledger_lock: str | None = Field(
        default=None,
        description="A script run as '<python> <script> <ledger> --append <row>' instead of the ledger command.",
    )
    ledger_command: tuple[str, ...] = Field(
        default=(),
        description="The onex-ledger command line, run as '<command> <ledger> --append <row>'.",
    )
    state_dir: str | None = Field(
        default=None,
        description="Where state.json and last-run.json are written; default under registry_root.",
    )
    python: str = Field(
        default="python3", description="Interpreter that runs the train and the lock."
    )
    osascript: str = Field(
        default="/usr/bin/osascript", description="The notification command."
    )
    now: str | None = Field(
        default=None, description="UTC timestamp used instead of the clock."
    )
    dry_run: bool = Field(
        default=False,
        description="Decide and report; dispatch nothing and write no row, state or notification.",
    )
    ticket: str = Field(
        default="",
        description="Ticket cell of the status row; omitted from the row when empty.",
    )


class ModelReleaseCutOutcome(BaseModel):
    """What one tick decided. ``action`` is one of the three actions of the decision module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["nothing-to-cut", "cut-dispatched", "blocked"]
    reason: str
    detail: str = ""
    candidate_sha: str = ""
    candidate_version: str = ""
    latest_tag: str = ""
    unreleased_count: int = 0
    oldest_unreleased_age_hours: float | None = None
    run_url: str = ""
    previous_run: str = ""


class ModelReleaseCutTickResult(BaseModel):
    """The outcome of one tick. ``status`` is success unless the tick was blocked (partial)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["success", "partial", "failure"]
    exit_code: int = Field(ge=0, le=3)
    outcome: ModelReleaseCutOutcome
    raised: bool = False
    dry_run: bool = False
    status_row: str = ""
    ledger_append: str = ""
    notified: bool | None = None
    log_line: str = ""
    error_message: str = ""
