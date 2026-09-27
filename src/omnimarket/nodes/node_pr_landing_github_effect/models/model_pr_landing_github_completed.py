# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Completion event of node_pr_landing_github_effect (OMN-19826)."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_pr_landing_github_effect.models.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.model_github_check_run_fact import (
    ModelGithubCheckRunFact,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.model_github_http_exchange import (
    ModelGithubHttpRequest,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.model_github_pr_state_fact import (
    ModelGithubPrStateFact,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.model_github_quota_reading import (
    ModelGithubQuotaReading,
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
    head_sha: str | None = Field(
        default=None,
        min_length=40,
        max_length=40,
        description="The request's head; None only on a read_pr_state sent before a head was known.",
    )
    requests: tuple[ModelGithubHttpRequest, ...] = Field(min_length=1)
    http_statuses: tuple[int, ...]
    not_modified: bool = False
    etag: str | None = None
    check_runs: tuple[ModelGithubCheckRunFact, ...] = ()
    pr_state: ModelGithubPrStateFact | None = Field(
        default=None,
        description=(
            "read_pr_state only: the snapshot read. None on a 304 (unchanged "
            "since the ETag) and on every other operation."
        ),
    )
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
        is_checks_read = op is EnumPrLandingGithubOperation.READ_HEAD_CHECKS
        is_state_read = op is EnumPrLandingGithubOperation.READ_PR_STATE
        is_read = is_checks_read or is_state_read
        if not is_read and (self.not_modified or self.etag):
            raise ValueError(
                "not_modified and etag are only valid on read_head_checks and read_pr_state"
            )
        if not is_checks_read and self.check_runs:
            raise ValueError("check_runs is only valid on read_head_checks")
        if not is_state_read and self.pr_state is not None:
            raise ValueError("pr_state is only valid on read_pr_state")
        if self.not_modified and (self.check_runs or self.pr_state is not None):
            raise ValueError(
                "a not_modified read carries no check_runs and no pr_state"
            )
        if (
            is_state_read
            and self.mode is EnumPrLandingGithubMode.ENFORCE
            and not self.not_modified
            and self.pr_state is None
        ):
            raise ValueError(
                "an enforce read_pr_state that was modified carries pr_state"
            )
        if self.pr_state is not None and self.pr_state.pr_number != self.pr_number:
            raise ValueError("pr_state is the snapshot of this request's PR")
        return self
