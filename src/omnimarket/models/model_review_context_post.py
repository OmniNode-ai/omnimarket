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


class ModelHeadStatus(BaseModel):
    """One commit status on a head: its id, context and state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_id: int
    context: str
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    state: str


class ModelReviewContextPostCommand(BaseModel):
    """The command to post a review-gate commit status."""

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
    status_id: int | None
    calls: int = Field(ge=0)
    detail: str
