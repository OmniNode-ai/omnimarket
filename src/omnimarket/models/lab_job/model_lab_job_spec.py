# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The submitted job: its original brief, placement, time box and retry policy.

The spec is the original instruction record of a job. It is stored on the job
row at submission (or at adoption, when the original instruction ledger holds
the lane's brief) and never rewritten: every restart is built from
``brief`` here, never from a previous attempt's continuation.
"""

from __future__ import annotations

import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_lab_job import (
    EnumLabJobDoneCriterionKind,
    EnumLabJobEngine,
    EnumLabJobKind,
    EnumLabJobOnTimeBox,
)

_JOB_ID = re.compile(r"^lj-[0-9a-f]{16}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class ModelLabJobRetryPolicy(BaseModel):
    """How many attempts a job gets, the wait between them, and time-box handling."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(default=2, ge=1, le=10)
    backoff_s: int = Field(default=60, ge=0, le=3600)
    on_time_box: EnumLabJobOnTimeBox = EnumLabJobOnTimeBox.CONTINUE


class ModelLabJobDoneCriterion(BaseModel):
    """One done criterion. ``target`` names the PR (``owner/name#n``) where needed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumLabJobDoneCriterionKind
    target: str | None = None
    check_context: str | None = None

    @model_validator(mode="after")
    def _target_when_needed(self) -> Self:
        needs_pr = {
            EnumLabJobDoneCriterionKind.PR_MERGED,
            EnumLabJobDoneCriterionKind.CHECK_PASSING,
        }
        if self.kind in needs_pr and not self.target:
            raise ValueError(f"done criterion {self.kind} needs a target PR")
        if (
            self.kind is EnumLabJobDoneCriterionKind.CHECK_PASSING
            and not self.check_context
        ):
            raise ValueError("done criterion check_passing needs a check_context")
        return self


class ModelLabJobSpec(BaseModel):
    """``onex.cmd.omnimarket.lab-job-submitted.v1`` payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    kind: EnumLabJobKind
    brief: str = Field(min_length=1)
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    ref: str
    engine: EnumLabJobEngine
    host_preference: tuple[str, ...] = ()
    time_box_min: int = Field(ge=1, le=180)
    retry_policy: ModelLabJobRetryPolicy = ModelLabJobRetryPolicy()
    done_criteria: tuple[ModelLabJobDoneCriterion, ...] = Field(min_length=1)
    parent_lane: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    stall_after_min: int = Field(default=20, ge=1, le=240)

    @model_validator(mode="after")
    def _ids(self) -> Self:
        if not _JOB_ID.match(self.job_id):
            raise ValueError(f"job_id must be lj-<16 hex>, got {self.job_id!r}")
        if not _SHA.match(self.ref):
            raise ValueError("ref must be a full 40-hex sha, resolved at submit")
        return self


__all__: list[str] = [
    "ModelLabJobDoneCriterion",
    "ModelLabJobRetryPolicy",
    "ModelLabJobSpec",
]
