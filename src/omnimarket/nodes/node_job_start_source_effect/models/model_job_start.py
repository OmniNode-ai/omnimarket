# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, source read, start record and result of the job-start source (OMN-22897).

A start is a job that began running: it carries the repository, the run and job ids, the runner group
and labels it ran on, the run's event and whether its head is a fork, and the time it started. A read
that could not be completed is a typed failure; it never becomes an empty list of starts.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingVerdict


class EnumJobStartFailure(StrEnum):
    """Why a read ended without the starts of the window."""

    ORG_READ_DENIED = "org-read-denied"
    RATE_LIMITED = "rate-limited"
    BUDGET_EXHAUSTED = "budget-exhausted"
    OPERATION_TIMEOUT = "operation-timeout"
    UNREADABLE = "unreadable"
    WINDOW_TRUNCATED = "window-truncated"
    READER_RAISED = "reader-raised"
    REPOSITORIES_INCOMPLETE = "repositories-incomplete"


class JobStartReaderError(Exception):
    """A reader that stops with a typed failure raises this; the handler turns it into a read."""

    def __init__(
        self,
        failure: EnumJobStartFailure,
        repository: str | None = None,
        calls: int = 0,
    ) -> None:
        super().__init__(failure.value)
        self.failure = failure
        self.repository = repository
        self.calls = calls


class ModelJobStart(BaseModel):
    """One job that started: where, which run, on which runner group, and when."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    run_id: int
    job_id: int
    job_name: str
    workflow_name: str
    run_event: str
    #: True when the run's head repository is not the repository the run belongs to.
    head_is_fork: bool
    repository_private: bool
    #: The runner group the job ran in; ``None`` when GitHub did not report one (a hosted runner).
    runner_group_name: str | None = None
    runner_name: str | None = None
    labels: tuple[str, ...] = ()
    started_at: datetime


class ModelJobStartRequest(BaseModel):
    """One read: the correlation id and the time the window ends at (no clock is read)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    now: datetime


class ModelJobStartRead(BaseModel):
    """What a reader returned, or the typed failure it ended in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    starts: tuple[ModelJobStart, ...] = ()
    #: The repositories whose starts are in ``starts`` (a repository read to its end).
    repositories_read: tuple[str, ...] = ()
    read_at: datetime
    calls: int = Field(ge=0)
    read_failure: EnumJobStartFailure | None = None
    failed_repository: str | None = None


class ModelJobStartSubject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    verdict: EnumReadingVerdict
    reason: str = ""
    count: int | None = None
    #: The runs the subject counts, when it counts runs (the public starts off the baseline).
    run_ids: tuple[int, ...] = ()


class ModelJobStartReading(BaseModel):
    """The reading of one read: WORKING only when every subject is, UNKNOWN when not read or stale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumReadingVerdict
    subjects: tuple[ModelJobStartSubject, ...]


class ModelJobStartStoreOutcome(BaseModel):
    """What the retained store did with the read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["written", "already-written", "not-written", "failed"]
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "failed"


class ModelJobStartResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    backend: Literal["inmemory", "github"]
    calls: int = Field(ge=0)
    window_start: datetime
    window_end: datetime
    read_at: datetime
    declared_repositories: tuple[str, ...]
    repositories_read: tuple[str, ...]
    #: Empty when the read failed: ``job_start_count`` says None then, never 0.
    starts: tuple[ModelJobStart, ...] = ()
    read_failure: EnumJobStartFailure | None = None
    failed_repository: str | None = None
    reading: ModelJobStartReading
    row_key: str
    store: ModelJobStartStoreOutcome | None = None

    @property
    def job_start_count(self) -> int | None:
        """The starts read in the window, or None when the window was not read."""
        return None if self.read_failure is not None else len(self.starts)


__all__ = [
    "EnumJobStartFailure",
    "JobStartReaderError",
    "ModelJobStart",
    "ModelJobStartRead",
    "ModelJobStartReading",
    "ModelJobStartRequest",
    "ModelJobStartResult",
    "ModelJobStartStoreOutcome",
    "ModelJobStartSubject",
]
