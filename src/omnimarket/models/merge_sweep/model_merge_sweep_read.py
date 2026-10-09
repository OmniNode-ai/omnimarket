# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the merge-sweep reading (OMN-20676).

The facts arrive already gathered from the PR watcher's state file, the rolling ledger and the
landing controller's tick log, by node_merge_sweep_effect. The watcher does not record a PR's
changed files, its last ``ready_for_review`` time or a merge's changed files: each such fact is
``None`` or listed in ``facts_unread``, and the reading names it under ``unread``. An unread fact
is never read as a zero.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ModelSweepRun(BaseModel):
    """The newest copy of one check name at a PR's head."""

    model_config = _FROZEN

    name: str
    id: int
    started_at: str | None = None
    status: str
    conclusion: str | None = None


class ModelSweepOpenPr(BaseModel):
    """An open PR as the watcher state holds it.

    ``runs`` is ``None`` when the state holds no check read at this head (``runs_why`` says why).
    ``files`` and ``ready_at`` come from a source richer than the state when one exists;
    ``facts_unread`` names those the source could not supply.
    """

    model_config = _FROZEN

    repo: str
    number: int
    title: str = ""
    base: str
    head_ref: str
    draft: bool = False
    runs: list[ModelSweepRun] | None = None
    runs_why: str = ""
    files: list[str] = Field(default_factory=list)
    ready_at: str | None = None
    facts_unread: list[str] = Field(default_factory=list)


class ModelSweepMerge(BaseModel):
    """A merge the watcher followed. ``files`` is ``None`` when its changed files were not read."""

    model_config = _FROZEN

    repo: str
    number: int
    merged_at: str | None = None
    files: list[str] | None = None


class ModelMergeSweepReadRequest(BaseModel):
    """Everything one reading needs, gathered; the time and the host load are arguments."""

    model_config = _FROZEN

    now: str
    window_min: int = 60
    max_reds: int = 40
    load1: float | None = None
    cpus: int | None = None
    floors: dict[str, float] = Field(default_factory=dict)
    open_prs: list[ModelSweepOpenPr] = Field(default_factory=list)
    merges: list[ModelSweepMerge] = Field(default_factory=list)
    ledger_lines: list[str] = Field(default_factory=list)
    ticks: list[dict[str, Any]] | None = None


class ModelMergeSweepUnread(BaseModel):
    """A fact the reading could not read, and why."""

    model_config = _FROZEN

    field: str
    why: str


class ModelMergeSweepReadResult(BaseModel):
    """One reading of the fleet, in the shape the lane plan consumes."""

    model_config = _FROZEN

    now: str
    load1: float | None
    cpus: int | None
    product: dict[str, Any]
    controller: dict[str, Any]
    reds: list[dict[str, Any]]
    chain_heads: list[dict[str, Any]]
    escalations: list[dict[str, Any]]
    open_prs: int
    unread: list[ModelMergeSweepUnread]


class ModelMergeSweepClaimCheckRequest(BaseModel):
    """The claim-time recheck of one PR: its state in the watcher state and the ledger rows."""

    model_config = _FROZEN

    pr: str
    now: str
    state: str | None = None
    title: str | None = None
    state_why: str = ""
    ledger_lines: list[str] = Field(default_factory=list)


class ModelMergeSweepClaimCheckResult(BaseModel):
    """FREE or STALE exits 0, a live owner 2, merged or closed 3, an unread state 4."""

    model_config = _FROZEN

    key: str
    state: str
    owner_state: str | None
    lane: str | None
    verdict: str
    exit_code: int
    line: str
