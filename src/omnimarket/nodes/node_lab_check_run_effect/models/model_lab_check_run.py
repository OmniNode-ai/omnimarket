# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of node_lab_check_run_effect: the request, the check runs it sees and the result (OMN-22950).

Rebuild plan 3.6 row 8 and J8: a lab-bound job posts a required check run on the exact head, carrying the identity of
the attempt that ran it. The identity rides in the run's ``external_id`` as one opaque string; this node only compares
it. Verdicts are the landing decision's own (``EnumLabCheckVerdict``), so a reading here and the decision's reading of
the same runs cannot name different things.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_lab_check import (
    EnumLabCheckVerdict,
)

SHA_PATTERN = r"^[0-9a-f]{40}$"
REPOSITORY_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"


class EnumLabCheckRunOperation(StrEnum):
    POST = "post"
    READ = "read"


class EnumLabCheckRunConclusion(StrEnum):
    """The state a post asks for: still running, passed or failed."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


class EnumLabCheckRunOutcome(StrEnum):
    POSTED = "posted"
    POSTED_LATE = "posted_late"
    READ = "read"
    REFUSED_SUPERSEDED_ATTEMPT = "refused_superseded_attempt"
    REFUSED_REPOSITORY_NOT_LAB_BOUND = "refused_repository_not_lab_bound"
    FAILED_READBACK_MISSING = "failed_readback_missing"
    FAILED_INVALIDATION_NOT_READ = "failed_invalidation_not_read"
    FAILED_API = "failed_api"


class ModelCheckRunRecord(BaseModel):
    """One check run as the check-run API gives it; ``attempt_id`` is its ``external_id``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_run_id: int
    repository: str = Field(..., pattern=REPOSITORY_PATTERN)
    name: str = Field(..., min_length=1)
    head_sha: str = Field(..., pattern=SHA_PATTERN)
    attempt_id: str
    #: GitHub's own status: ``queued``, ``in_progress`` or ``completed``.
    status: str = Field(..., min_length=1)
    #: GitHub's own conclusion, absent until the run is completed.
    conclusion: str | None = None


class ModelLabCheckRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., min_length=1)
    operation: EnumLabCheckRunOperation
    repository: str = Field(..., pattern=REPOSITORY_PATTERN)
    head_sha: str = Field(..., pattern=SHA_PATTERN)
    #: The attempt the order says is current; the only attempt whose check counts.
    current_attempt_id: str = Field(..., min_length=1)
    #: When the lab job was dispatched: the wait bound runs from here.
    dispatched_at: datetime
    now: datetime
    #: A post: the attempt that ran the job and the state it reports.
    attempt_id: str | None = Field(default=None, min_length=1)
    conclusion: EnumLabCheckRunConclusion | None = None
    detail: str = ""

    @model_validator(mode="after")
    def _post_names_attempt_and_state(self) -> ModelLabCheckRunRequest:
        if self.operation is EnumLabCheckRunOperation.POST and (
            self.attempt_id is None or self.conclusion is None
        ):
            raise ValueError("a post names the attempt that ran the job and its state")
        return self


class ModelLabCheckRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    operation: EnumLabCheckRunOperation
    outcome: EnumLabCheckRunOutcome
    backend: str
    repository: str
    head_sha: str
    check_name: str
    #: The run this attempt posted, read back from the API after the post.
    posted: ModelCheckRunRecord | None = None
    #: Runs of other attempts the post took the pass away from.
    invalidated: tuple[int, ...] = ()
    #: What the head's runs read as after the operation; never PASS unless a current-attempt run passed on the head.
    verdict: EnumLabCheckVerdict | None = None
    within_wait_bound: bool
    calls: int = Field(ge=0)
    detail: str = ""


__all__ = [
    "REPOSITORY_PATTERN",
    "SHA_PATTERN",
    "EnumLabCheckRunConclusion",
    "EnumLabCheckRunOperation",
    "EnumLabCheckRunOutcome",
    "ModelCheckRunRecord",
    "ModelLabCheckRunRequest",
    "ModelLabCheckRunResult",
]
