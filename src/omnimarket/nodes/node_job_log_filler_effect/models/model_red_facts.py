# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The red-facts record of node_job_log_filler_effect (OMN-23832).

After the filler holds a red job's log it writes one typed record per repository, pull request,
head commit and job: the check, the job id, the failing step, a capped digest of the failing
lines and where the log is cached. ``onex scm red-facts`` answers from these records alone, so a
lane learns the failing line without a GitHub read. The head commit is part of the key, so a
new head never reads the old head's text: it reads ``not_filled``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelRedFactsRecord(BaseModel):
    """What the filler learned from one red job's log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(description="owner/name")
    pr_number: int = Field(ge=1)
    head_sha: str
    check: str = Field(description="The job (check) name.")
    job_id: int = Field(ge=1)
    failing_step: str | None = Field(
        default=None, description="The step whose command ended in the first error."
    )
    digest_lines: tuple[str, ...] = Field(
        description="Capped failure lines, after redaction."
    )
    digest_kind: Literal["junit", "log_lines"]
    cache_path: str | None = Field(
        default=None, description="The job-log cache root the log was read from."
    )
    log_total_bytes: int = Field(ge=0)
    recorded_at: str = Field(description="UTC, ISO 8601 with a trailing Z.")


class EnumRedFactsStatus(StrEnum):
    FOUND = "found"
    NOT_FILLED = "not_filled"


class ModelRedFactsAnswer(BaseModel):
    """The answer of ``onex scm red-facts`` for a pull request's current head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumRedFactsStatus
    repo: str
    pr_number: int = Field(ge=1)
    head_sha: str = Field(description="The pull request's current head.")
    records: tuple[ModelRedFactsRecord, ...] = ()
    reason: str | None = None
    head_age_seconds: int | None = Field(
        default=None,
        description="Seconds since the watcher last read this head's checks, when known.",
    )


__all__ = [
    "EnumRedFactsStatus",
    "ModelRedFactsAnswer",
    "ModelRedFactsRecord",
]
