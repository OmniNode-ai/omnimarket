# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Completion event of node_pr_landing_github_effect (OMN-19826, contract 1.1.0 by OMN-19831).

Contract 1.1.0 adds ``pr_state`` (read_pr_state, and the read an arm or
enqueue makes before its mutation), ``started_attempts`` (rerun_runs, F7),
the conditional read fields on read_pr_state, and lets ``head_sha`` be None on
a read_pr_state that was asked without one.
"""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.events.pr_landing_github.model_github_check_run_fact import (
    ModelGithubCheckRunFact,
)
from omnimarket.events.pr_landing_github.model_github_pr_state_fact import (
    ModelGithubPrStateFact,
)
from omnimarket.events.pr_landing_github.model_github_quota_reading import (
    ModelGithubQuotaReading,
)
from omnimarket.events.pr_landing_github.model_github_run_attempt import (
    ModelGithubRunAttempt,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
)

_CONDITIONAL_READS = frozenset(
    {
        EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
        EnumPrLandingGithubOperation.READ_PR_STATE,
    }
)
_PR_STATE_OPERATIONS = frozenset(
    {
        EnumPrLandingGithubOperation.READ_PR_STATE,
        EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
        EnumPrLandingGithubOperation.ENQUEUE,
    }
)


class ModelPrLandingGithubCompleted(BaseModel):
    """An operation finished (enforce) or its requests were recorded (dry_run).

    enforce: one HTTP status per request and the last response's quota reading.
    dry_run: the recorded requests only; no status and no quota, because
    nothing was sent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    operation: EnumPrLandingGithubOperation
    mode: EnumPrLandingGithubMode
    repository: str
    pr_number: int = Field(gt=0)
    head_sha: str | None = Field(default=None, min_length=40, max_length=40)
    requests: tuple[ModelGithubHttpRequest, ...] = Field(min_length=1)
    http_statuses: tuple[int, ...]
    not_modified: bool = False
    etag: str | None = None
    check_runs: tuple[ModelGithubCheckRunFact, ...] = ()
    pr_state: ModelGithubPrStateFact | None = None
    started_attempts: tuple[ModelGithubRunAttempt, ...] = ()
    quota: ModelGithubQuotaReading | None

    @model_validator(mode="after")
    def _mode_rules(self) -> Self:
        if self.mode is EnumPrLandingGithubMode.DRY_RUN:
            if self.http_statuses or self.quota is not None:
                raise ValueError(
                    "a dry_run result records requests only: no http_statuses, no quota"
                )
        else:
            if self.quota is None:
                raise ValueError(
                    "an enforce result must carry the header quota reading"
                )
            if len(self.http_statuses) != len(self.requests):
                raise ValueError(
                    "an enforce result needs one of http_statuses per request"
                )
        op = self.operation
        if op not in _CONDITIONAL_READS and (self.not_modified or self.etag):
            raise ValueError(
                "not_modified and etag are only valid on read_head_checks and read_pr_state"
            )
        if op is not EnumPrLandingGithubOperation.READ_HEAD_CHECKS and self.check_runs:
            raise ValueError("check_runs are only valid on read_head_checks")
        if op not in _PR_STATE_OPERATIONS and self.pr_state is not None:
            raise ValueError(
                "pr_state is only valid on read_pr_state, arm_auto_merge and enqueue"
            )
        if op is not EnumPrLandingGithubOperation.RERUN_RUNS and self.started_attempts:
            raise ValueError("started_attempts are only valid on rerun_runs")
        if self.not_modified and (self.check_runs or self.pr_state is not None):
            raise ValueError(
                "a not_modified read carries no check_runs and no pr_state"
            )
        if (
            self.head_sha is None
            and op is not EnumPrLandingGithubOperation.READ_PR_STATE
        ):
            raise ValueError(f"a {op.value} result must name its head_sha")
        return self
