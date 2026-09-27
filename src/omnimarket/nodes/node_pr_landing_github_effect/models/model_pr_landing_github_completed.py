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
    head_sha: str = Field(min_length=40, max_length=40)
    requests: tuple[ModelGithubHttpRequest, ...] = Field(min_length=1)
    http_statuses: tuple[int, ...]
    not_modified: bool = False
    etag: str | None = None
    check_runs: tuple[ModelGithubCheckRunFact, ...] = ()
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
        is_read = self.operation is EnumPrLandingGithubOperation.READ_HEAD_CHECKS
        if not is_read and (self.not_modified or self.etag or self.check_runs):
            raise ValueError(
                "not_modified, etag and check_runs are only valid on read_head_checks"
            )
        if self.not_modified and self.check_runs:
            raise ValueError("a not_modified read carries no check_runs")
        return self
