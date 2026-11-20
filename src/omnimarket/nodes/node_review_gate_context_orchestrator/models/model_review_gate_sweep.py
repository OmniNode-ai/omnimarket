# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command and result models of the review-gate context sweep."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_review_gate_context import (
    EnumReviewContextReason,
    EnumReviewContextState,
)


class ModelReviewGateSweepCommand(BaseModel):
    """One sweep: read the open pull requests and the recorded review verdicts, decide and post."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    repositories: tuple[str, ...] = ()
    post: bool


class ModelReviewGateSweepEntry(BaseModel):
    """The decision of one pull request head in a sweep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    pr_number: int
    head_sha: str
    state: EnumReviewContextState
    reason: EnumReviewContextReason
    outcome: str


class ModelReviewGateSweepResult(BaseModel):
    """The result of a sweep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    ok: bool
    unread: str | None
    entries: tuple[ModelReviewGateSweepEntry, ...]
    posted: int
    unchanged: int
    failed: int
