# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of one whole merge sweep (OMN-20676).

The request names where the sweep's facts are and how its lanes are started. Nothing about the
deployment is packaged: the state, ledger, tick-log and floors paths, the runner script, the brief
directory, the sweep's own lane and parent, the ticket and the model all arrive on the request, read
by the caller from its overlay.

``supplement`` carries the facts the PR watcher's state does not record (a PR's changed files, its
last ready_for_review time, a merge's changed files) when the caller has a richer source. Without
it those facts stay unread and the reading names them under ``unread``; an unread merge never
counts as a product merge.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.merge_sweep import ModelMergeSweepUnread
from omnimarket.models.merge_sweep.model_merge_sweep_effect import MAX_AGE_S
from omnimarket.models.merge_sweep.model_merge_sweep_plan import (
    ModelMergeSweepPlanResult,
    ModelMergeSweepSkipped,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ModelMergeSweepSupplement(BaseModel):
    """Facts a richer source than the watcher state supplies, keyed ``<repo>#<n>``."""

    model_config = _FROZEN

    files: dict[str, list[str]] = Field(default_factory=dict)
    ready_at: dict[str, str] = Field(default_factory=dict)
    merge_files: dict[str, list[str]] = Field(default_factory=dict)


class ModelMergeSweepRunRequest(BaseModel):
    """One merge sweep: where its facts are, what it dispatches with, and its limits.

    ``max_lanes``, ``retries`` and ``retry_wait_min`` read None as the default and clamp a value
    out of range, as the sweep read its arguments. ``dispatch`` false plans the sweep and starts
    no lane.
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
    supplement: ModelMergeSweepSupplement | None = None
    lane: str
    parent: str
    ticket: str
    model: str
    runner_script: str
    brief_dir: str
    host: str | None = None
    max_lanes: int | None = None
    retries: int | None = None
    retry_wait_min: int | None = None
    claim_check_command: str | None = None
    dispatch: bool = True


class ModelMergeSweepRetriedAfter(BaseModel):
    """The receipt of the first start of a lane that was started again on another host."""

    model_config = _FROZEN

    host: str | None
    status: str
    receipt: str


class ModelMergeSweepLaneOutcome(BaseModel):
    """One dispatched lane: its final receipt and every start and wait it took, in order."""

    model_config = _FROZEN

    lane: str
    kind: str
    repo: str | None
    prs: list[str]
    exit_code: int
    status: str
    host: str | None
    retried_after: ModelMergeSweepRetriedAfter | None
    events: list[str]


class ModelMergeSweepRunResult(BaseModel):
    """The sweep: why it was refused, or what it planned and dispatched.

    ``complete`` is false when the reading names facts it could not read, so a caller does not take
    a plan made over unread facts for a plan made over the whole fleet.
    """

    model_config = _FROZEN

    ok: bool
    why: str = ""
    complete: bool = False
    unread: list[ModelMergeSweepUnread] = Field(default_factory=list)
    plan: ModelMergeSweepPlanResult | None = None
    skipped: list[ModelMergeSweepSkipped] = Field(default_factory=list)
    deferred: int = 0
    outcomes: list[ModelMergeSweepLaneOutcome] = Field(default_factory=list)
