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


class ModelQueuePoolHost(BaseModel):
    """One merge-queue pool of the overlay: its runners' name prefix and the host that carries it (OMN-23387)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runner_name_prefix: str = Field(min_length=1)
    host: str = Field(min_length=1)


class ModelRunnerStopIdleProofSettings(BaseModel):
    """The overlay's settings and the caller's injected stop set; no default here is a policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_set_labels: tuple[str, ...] = Field(min_length=1)
    proof_group_ids: tuple[int, ...] = Field(min_length=1)
    required_reads: int = Field(ge=2)
    min_gap_seconds: int = Field(gt=0)
    max_age_seconds: int = Field(gt=0)
    # OMN-23156: the names of the queue runners that may stop only after the replacement pool is proven. Empty
    # means the caller named none and the queue-pool subject is not judged.
    queue_pool_stop_name_prefixes: tuple[str, ...] = ()
    queue_pool_proof_max_age_seconds: int = Field(default=1800, gt=0)
    # OMN-23387: the host of each merge-queue pool. When declared, a stop request that names a queue runner is refused
    # if the queue runners left online would sit on fewer than queue_pool_min_hosts hosts. A floor of two.
    queue_pool_hosts: tuple[ModelQueuePoolHost, ...] = ()
    queue_pool_min_hosts: int = Field(default=2, ge=2)


class ModelQueuePoolProof(BaseModel):
    """The replacement queue pool's reading: its verdict, when it was read and the merge-group run that proved it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_at: datetime
    verdict: EnumReadingVerdict
    proven_run_id: int | None = None


class ModelRunnerStopIdleProofRequest(BaseModel):
    """The reads, the settings, and the time the proof is judged at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    now: datetime
    settings: ModelRunnerStopIdleProofSettings
    reads: tuple[ModelProofRead, ...] = ()
    stop_request_names: tuple[str, ...] = ()
    queue_pool_proof: ModelQueuePoolProof | None = None
    # OMN-23387: the names of the queue runners online at the read; None when the pool was not read.
    queue_runners_online: tuple[str, ...] | None = None


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
    refused_stop_names: tuple[str, ...] = ()
