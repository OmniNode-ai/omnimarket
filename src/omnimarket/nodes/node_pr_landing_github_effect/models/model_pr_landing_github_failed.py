# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failure event of node_pr_landing_github_effect (OMN-19826)."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_pr_landing_github_effect.models.enum_pr_landing_github_failure_reason import (
    EnumPrLandingGithubFailureReason,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.model_github_quota_reading import (
    ModelGithubQuotaReading,
)


class ModelPrLandingGithubFailed(BaseModel):
    """An operation did not complete, with a typed reason.

    Every failure carries a header quota reading except ``transport_error``,
    where no response arrived. A ``quota_floor`` refusal made no call, so it has
    no HTTP status and carries the reading that triggered it. dry_run calls
    nothing and so never fails against GitHub.
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
    reason: EnumPrLandingGithubFailureReason
    detail: str = Field(min_length=1)
    http_status: int | None
    retry_after_seconds: int | None = Field(default=None, ge=0)
    quota: ModelGithubQuotaReading | None

    @model_validator(mode="after")
    def _reason_rules(self) -> Self:
        if self.mode is EnumPrLandingGithubMode.DRY_RUN:
            raise ValueError(
                "dry_run records requests and calls nothing; it cannot fail"
            )
        no_response = self.reason is EnumPrLandingGithubFailureReason.TRANSPORT_ERROR
        if self.quota is None and not no_response:
            raise ValueError(
                f"a {self.reason.value} failure must carry the header quota reading"
            )
        if self.reason is EnumPrLandingGithubFailureReason.QUOTA_FLOOR and (
            self.http_status is not None
        ):
            raise ValueError(
                "a quota_floor refusal made no call and has no http_status"
            )
        if no_response and self.http_status is not None:
            raise ValueError("a transport_error has no http_status")
        return self
