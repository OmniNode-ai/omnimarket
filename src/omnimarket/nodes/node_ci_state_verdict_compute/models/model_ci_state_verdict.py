# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the ci-watch verdict decisions (OMN-20686)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelCiStateCheckRun(BaseModel):
    """One check-run copy at the head, as the caller read it (status and conclusion lower-case)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    name: str
    status: str | None = None
    conclusion: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    details_url: str | None = None
    suite: int | None = None


class ModelCiStateStatus(BaseModel):
    """The latest legacy commit status of one context at the head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: str
    state: str
    created_at: str


class ModelCiStateCheckSuite(BaseModel):
    """One check suite GitHub created at the head: a GitHub App's umbrella object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    app: str | None = None
    status: str | None = None
    conclusion: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ModelCiStateWatcherFacts(BaseModel):
    """The PR facts the PR watcher holds for one PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: str | None = None
    draft: bool = False
    head_sha: str | None = None
    base: str | None = None
    labels: list[str] = Field(default_factory=list)
    armed: bool = False


class ModelCiStateWatcherCi(BaseModel):
    """The CI the PR watcher read at one head: its verdict and the newest copy of each check-run name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str | None = None
    verdict: str | None = None
    runs: list[list[str | int | float | bool | None]] = Field(
        default_factory=list,
        description="[name, status, conclusion, when] per check-run name.",
    )
    red: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    read_at: str | None = None
    total: int | None = None


class ModelCiStateWatcher(BaseModel):
    """The watcher's record for the PR: its facts and, when it has read CI, that CI."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: ModelCiStateWatcherFacts
    ci: ModelCiStateWatcherCi | None = None


class ModelCiStateVerdictRequest(BaseModel):
    """What a caller read before ci-watch decides: the watcher record, or a GitHub read, or a log.

    The handler reads nothing, runs nothing and has no clock: ``now`` is the read time the caller
    stamps. A fact the caller could not read is ``None``, never a guess.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["watcher", "live", "classify-log"]
    full: str = Field(default="", description="owner/repo of the PR.")
    number: str = Field(default="", description="The PR number as text.")
    now: str = Field(
        default="1970-01-01T00:00:00+00:00",
        description="ISO 8601 UTC read time; the read_at stamp and the clock for stuck check suites.",
    )
    watcher: ModelCiStateWatcher | None = Field(
        default=None, description="operation=watcher: the watcher's record for the PR."
    )
    watcher_unavailable: str | None = Field(
        default=None,
        description="operation=watcher: why the caller could not use the watcher state at all "
        "(script missing, state unloadable, PR absent from the state).",
    )
    reads_line: str = Field(
        default="", description="operation=watcher: the watcher's own reads line."
    )
    live_flag: bool = Field(
        default=False, description="operation=live: the caller passed --live."
    )
    watcher_not_used_reason: str | None = Field(
        default=None,
        description="operation=live without --live: why the watcher state was not used.",
    )
    reader_lines: list[str] = Field(
        default_factory=list,
        description="operation=live: the lines the GitHub reader printed while reading.",
    )
    pr: dict[str, Any] = Field(
        default_factory=dict,
        description="operation=live: the PR record, key order kept; written to pr.json.",
    )
    required: dict[str, str] = Field(
        default_factory=dict,
        description="operation=live: required context to the source that requires it.",
    )
    check_runs: list[ModelCiStateCheckRun] = Field(default_factory=list)
    check_runs_total: int = 0
    statuses: list[ModelCiStateStatus] = Field(default_factory=list)
    workflow_runs: list[list[str]] = Field(
        default_factory=list,
        description="[id, name, event, status, conclusion or -, run_attempt, created_at] per workflow run.",
    )
    check_suites: list[ModelCiStateCheckSuite] = Field(default_factory=list)
    check_suites_read: bool = Field(
        default=False,
        description="True once the caller read the check suites after a needs_check_suites result.",
    )
    log_text: str = Field(
        default="", description="operation=classify-log: the failing log."
    )


class ModelCiStateVerdictResult(BaseModel):
    """What ci-watch prints, which scratch files it writes and how it exits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["answer", "read-github", "read-check-suites"]
    exit_code: int | None = Field(
        default=None,
        description="0 GREEN, 1 RED, 2 PENDING, 3 the read could not be trusted; None when there is no answer yet.",
    )
    stdout_lines: list[str] = Field(default_factory=list)
    stderr_lines: list[str] = Field(default_factory=list)
    files: dict[str, str] = Field(
        default_factory=dict,
        description="Scratch-directory file name to its content.",
    )
    fallback_reason: str | None = Field(
        default=None,
        description="action=read-github: why the watcher state was not used.",
    )
    verdict: str | None = None
