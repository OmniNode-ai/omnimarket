# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the runner-stop idle proof (OMN-22952).

The request carries reads a fetching effect already took (the runners of the org with their group and
busy flag, the queued jobs with the labels they select) and the settings from the overlay. The result
is one verdict per subject and one overall verdict. Nothing here reads a clock or a network, and no
label or group is named in this repository: the caller injects both.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingVerdict


class EnumIdleProofReadFailure(StrEnum):
    """Why the fetching effect could not take one read; a failed read reads UNKNOWN."""

    ORG_READ_DENIED = "org-read-denied"
    BUDGET_EXHAUSTED = "budget-exhausted"
    UNREADABLE = "unreadable"


class ModelProofRunner(BaseModel):
    """One runner of the org: its group and whether it was running a job at the read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    group_id: int
    busy: bool
    labels: tuple[str, ...] = ()


class ModelProofQueuedJob(BaseModel):
    """One job queued at the read and the runner labels its workflow selects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: int
    repository: str | None = None
    labels: tuple[str, ...] = ()


class ModelProofRead(BaseModel):
    """One read: when it was taken, which proof groups it covered, the runners and the queued jobs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_at: datetime
    groups_read: tuple[int, ...] = ()
    runners: tuple[ModelProofRunner, ...] = ()
    queued_jobs: tuple[ModelProofQueuedJob, ...] = ()
    read_failure: EnumIdleProofReadFailure | None = None


class ModelRunnerStopIdleProofSettings(BaseModel):
    """The overlay's settings and the caller's injected stop set; no default here is a policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_set_labels: tuple[str, ...] = Field(min_length=1)
    proof_group_ids: tuple[int, ...] = Field(min_length=1)
    required_reads: int = Field(ge=2)
    min_gap_seconds: int = Field(gt=0)
    max_age_seconds: int = Field(gt=0)


class ModelRunnerStopIdleProofRequest(BaseModel):
    """The reads, the settings, and the time the proof is judged at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    now: datetime
    settings: ModelRunnerStopIdleProofSettings
    reads: tuple[ModelProofRead, ...] = ()


class ModelIdleProofSubject(BaseModel):
    """One subject of the proof: its verdict, its count and the reason in words."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    verdict: EnumReadingVerdict
    count: int | None = None
    reason: str


class ModelRunnerStopIdleProofResult(BaseModel):
    """The proof: one overall verdict, the subjects behind it, and what blocked it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    verdict: EnumReadingVerdict
    subjects: tuple[ModelIdleProofSubject, ...]
    reads_judged: int
    busy_runners: tuple[str, ...] = ()
    blocking_job_ids: tuple[int, ...] = ()
