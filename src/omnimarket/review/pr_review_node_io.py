# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared node-I/O command/event models for the PR-review node family (OMN-13212 / B2).

OWNER module for the request/result models exchanged between
node_pr_review_orchestrator and the PR-review COMPUTE/EFFECT nodes
(node_github_review_effect, node_judge_verdict_parse_compute). Living in shared
``omnimarket.review`` means no node reaches into a sibling node's private models
package (enforced by tests/test_no_cross_node_reach_in.py).
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.review.pr_review_io import ReviewFinding, ReviewVerdict, ThreadState

# ---------------------------------------------------------------------------
# GitHub review EFFECT I/O — post threads / watch / report
# ---------------------------------------------------------------------------


class EnumGithubReviewOperation(StrEnum):
    """Which GitHub-side operation the effect should perform."""

    POST_THREADS = "post_threads"
    WATCH_THREADS = "watch_threads"
    POST_REPORT = "post_report"


class ModelGithubReviewCommand(BaseModel):
    """Command to perform one GitHub review-side I/O operation.

    A single EFFECT covering thread post, resolution polling, and the final
    report comment. The orchestrator selects the operation; the effect performs
    the I/O and returns the updated thread states (or a posted-report event).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Review run correlation ID.")
    operation: EnumGithubReviewOperation = Field(...)
    repo: str = Field(..., min_length=1, description="GitHub repo 'owner/name'.")
    pr_number: int = Field(..., ge=1)
    dry_run: bool = Field(
        default=False, description="If true, perform no GitHub writes."
    )
    max_findings_per_pr: int = Field(default=20, ge=1)
    findings: tuple[ReviewFinding, ...] = Field(default_factory=tuple)
    thread_states: tuple[ThreadState, ...] = Field(default_factory=tuple)
    verdict: ReviewVerdict | None = Field(
        default=None, description="Verdict to post (POST_REPORT operation only)."
    )


class ModelGithubReviewResultEvent(BaseModel):
    """Result of a GitHub review-side I/O operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    operation: EnumGithubReviewOperation = Field(...)
    repo: str = Field(...)
    pr_number: int = Field(...)
    thread_states: tuple[ThreadState, ...] = Field(default_factory=tuple)
    report_comment_id: int | None = Field(
        default=None, description="Posted PR comment ID (POST_REPORT)."
    )


# ---------------------------------------------------------------------------
# Judge verdict parse COMPUTE I/O — pure PASS/FAIL parse
# ---------------------------------------------------------------------------


class ModelJudgeParseRequest(BaseModel):
    """Request to parse one judge model's raw PASS/FAIL response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Review run correlation ID.")
    raw_text: str = Field(..., description="Raw judge LLM response text to parse.")


class ModelJudgeParseResult(BaseModel):
    """Parsed judge verdict: PASS/FAIL + reasoning.

    Malformed JSON or an unknown verdict is treated as FAIL with a clear
    reasoning message (fail-closed); ``passed`` is the only authority field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool = Field(..., description="True iff the judge returned PASS.")
    reasoning: str = Field(
        ..., min_length=1, description="Judge reasoning or parse error."
    )


__all__: list[str] = [
    "EnumGithubReviewOperation",
    "ModelGithubReviewCommand",
    "ModelGithubReviewResultEvent",
    "ModelJudgeParseRequest",
    "ModelJudgeParseResult",
]
