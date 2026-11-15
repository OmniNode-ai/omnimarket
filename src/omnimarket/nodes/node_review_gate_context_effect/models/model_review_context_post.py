# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command and result models for node_review_gate_context_effect."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_review_gate_context import (
    EnumReviewContextState,
    ModelReviewGateContextDecision,
)


class EnumReviewContextPostOutcome(StrEnum):
    """The outcome of a review context post."""

    POSTED = "posted"
    UNCHANGED = "unchanged"
    FAILED_READ = "failed_read"
    FAILED_POST = "failed_post"
    FAILED_READBACK = "failed_readback"


class ModelHeadCheckRun(BaseModel):
    """One check run on a head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_run_id: int
    name: str
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    status: str
    conclusion: str | None


class ModelReviewContextPostCommand(BaseModel):
    """The command to post a review context check run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    decision: ModelReviewGateContextDecision


class ModelReviewContextPostResult(BaseModel):
    """The result of a review context post."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumReviewContextPostOutcome
    repository: str
    pr_number: int
    head_sha: str
    check_name: str
    state: EnumReviewContextState
    check_run_id: int | None
    calls: int = Field(ge=0)
    detail: str
