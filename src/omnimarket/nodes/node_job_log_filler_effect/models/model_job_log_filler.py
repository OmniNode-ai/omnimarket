# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O and overlay of node_job_log_filler_effect (OMN-23032).

The filler reads the PR watcher's state, picks the jobs that decide a merge, and has one
reader fetch each job's log into the job-log cache the gateway's ``job-log-cached`` read
answers from. Every deployment setting lives in :class:`ModelJobLogFillerOverlay`; no key
has a default, so an absent key refuses the load and names it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

OVERLAY_ID = "node_job_log_filler_effect"
OVERLAY_FILE_ENV = "ONEX_JOB_LOG_FILLER_OVERLAY"


class ModelJobLogFillerBusProfile(BaseModel):
    """The in-memory default (readers run here) or the Redpanda lane (readers ask the gateway)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["inmemory", "kafka"]
    lane: str | None = Field(default=None, min_length=1)


class ModelJobLogFillerOverlay(BaseModel):
    """Every setting a fill pass reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Owner the watcher's bare repository names belong to (``omnimarket`` -> ``<owner>/omnimarket``).
    github_owner: str = Field(min_length=1)
    #: Environment variable naming the PR watcher's state file on this host.
    watcher_state_env: str = Field(min_length=1)
    #: Watcher classes whose open pull requests have merge-relevant runs.
    pr_classes: tuple[str, ...] = Field(min_length=1)
    #: Jobs that decide a merge even when green, matched by exact name (may be empty).
    always_job_names: tuple[str, ...]
    #: Most live log reads one pass makes; the rest wait for the next pass.
    max_reads_per_pass: int = Field(ge=1)
    #: Readers running at once.
    reader_concurrency: int = Field(ge=1)
    #: Bytes of the log's end a read keeps in its reply (the cache keeps the whole log).
    reply_tail_bytes: int = Field(ge=1)
    reply_timeout_seconds: float = Field(gt=0)
    #: A log a pass confirmed cached is not probed again until this many seconds have passed. A probe over
    #: the bus is a round trip the gateway answers one at a time (measured 2026-11-09: about 0.25 s each,
    #: 841 probes in about four minutes), so probing every open run on every tick does not fit a tick.
    reprobe_seconds: int = Field(ge=1)
    #: Wall-clock seconds after which a pass starts no further probe or read and returns what it holds (OMN-22518).
    #: A probe is a round trip the gateway answers one at a time (measured 2026-11-09 on h051: median 2.4 s with
    #: six readers, 886 runs to probe), so a pass that probes every run before it fills any never ended inside
    #: the unit's run limit. The budget plus ``reply_timeout_seconds`` (the longest read in flight when it ends)
    #: must stay under the unit's ``run_timeout_s``; the runs a pass leaves are taken, newest first, by the next.
    pass_budget_seconds: float = Field(default=240.0, gt=0)
    profiles: dict[str, ModelJobLogFillerBusProfile] = Field(min_length=1)
    default_profile: str = Field(min_length=1)

    def profile(self, name: str | None) -> ModelJobLogFillerBusProfile:
        key = name or self.default_profile
        if key not in self.profiles:
            raise ValueError(
                f"no bus profile {key!r}; the overlay has {sorted(self.profiles)}"
            )
        return self.profiles[key]


class EnumJobLogFillReason(StrEnum):
    RED = "red"
    NAMED = "named"


class ModelMergeRelevantRun(BaseModel):
    """One job of an open pull request's current head that decides its merge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(description="owner/name")
    pr_number: int = Field(ge=1)
    head_sha: str
    job_name: str
    job_id: int = Field(ge=1)
    reason: EnumJobLogFillReason


class EnumJobLogFillOutcome(StrEnum):
    FILLED = "filled"
    ALREADY_CACHED = "already_cached"
    FAILED = "failed"


class ModelJobLogFillItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run: ModelMergeRelevantRun
    outcome: EnumJobLogFillOutcome
    total_bytes: int | None = None
    error: str | None = Field(default=None, max_length=2000)


class EnumJobLogFillStatus(StrEnum):
    """``completed``: no read failed. ``partial``: some failed. ``failed``: all failed or the pass could not start."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class ModelJobLogFillRequest(BaseModel):
    """One fill pass; every limit comes from the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(default_factory=uuid4)
    profile: str | None = None
    repos: tuple[str, ...] = Field(
        default=(), description="Narrow the pass to these owner/name repositories."
    )
    max_reads: int | None = Field(
        default=None,
        ge=1,
        description="Lower the overlay's per-pass cap, never raise it.",
    )


class ModelJobLogFillResult(BaseModel):
    """The pass's terminal event payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    status: EnumJobLogFillStatus
    profile: str
    watcher_tick: str | None = None
    cache_path: str | None = Field(
        default=None,
        description="The job-log cache root the in-memory profile filled; a gateway's root is its own.",
    )
    considered: int = Field(ge=0, description="Merge-relevant runs found.")
    filled: int = Field(ge=0)
    already_cached: int = Field(ge=0)
    failed: int = Field(ge=0)
    deferred: int = Field(
        ge=0,
        description="Runs left for the next pass by the read cap or the pass budget.",
    )
    budget_exhausted: bool = Field(
        default=False,
        description="True when the pass budget ended the pass before every run was reached.",
    )
    stale_skipped: int = Field(
        ge=0, description="Pull requests whose CI record is not of their current head."
    )
    items: tuple[ModelJobLogFillItem, ...] = ()
    error: str | None = Field(default=None, max_length=2000)


__all__ = [
    "OVERLAY_FILE_ENV",
    "OVERLAY_ID",
    "EnumJobLogFillOutcome",
    "EnumJobLogFillReason",
    "EnumJobLogFillStatus",
    "ModelJobLogFillItem",
    "ModelJobLogFillRequest",
    "ModelJobLogFillResult",
    "ModelJobLogFillerBusProfile",
    "ModelJobLogFillerOverlay",
    "ModelMergeRelevantRun",
]
