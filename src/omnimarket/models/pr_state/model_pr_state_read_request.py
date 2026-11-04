# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests to the PR-state read node: judge one page, or wait for a verdict."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.pr_state.model_pr_state_projection_page import (
    ModelPrStateProjectionPage,
)
from omnimarket.models.pr_state.model_pr_state_read_policy import (
    ModelPrStateReadPolicy,
)


class ModelPrStateReadRequest(BaseModel):
    """Judge the row for one pull request in an already-fetched page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    pr_number: int = Field(gt=0)
    head_sha: str | None = None
    page: ModelPrStateProjectionPage
    now: datetime
    policy: ModelPrStateReadPolicy


class ModelPrStateWaitRequest(BaseModel):
    """Wait for the row of one pull request to reach a terminal verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    pr_number: int = Field(gt=0)
    head_sha: str | None = None
    timeout_seconds: float = Field(gt=0)
    policy: ModelPrStateReadPolicy
