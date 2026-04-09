"""Pydantic models for node_pr_review_bot.

Models covering the full PR review bot lifecycle:
- ReviewRequest: input command payload
- DiffHunk: a single file diff segment
- ReviewFinding: a single finding from the reviewer model
- ReviewVerdict: aggregated result for a PR review run
- ThreadState: GitHub review thread lifecycle tracking
- EnumFsmPhase: FSM state machine phases
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    EnumFindingCategory,
    EnumFindingSeverity,
    EnumReviewConfidence,
    ModelReviewFinding,
)

# ---------------------------------------------------------------------------
# PR-bot-specific evidence model with line anchors
# ---------------------------------------------------------------------------


class PrReviewFindingEvidence(BaseModel):
    """Evidence model for PR review bot findings, extending the base with line anchors.

    HandlerThreadPoster uses line_start / line_end to anchor GitHub review
    threads to specific diff lines. The shared ModelFindingEvidence does not
    carry these fields, so we define a PR-bot-specific evidence model here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str | None = Field(
        default=None, description="Repo-relative file path of the finding."
    )
    line_start: int | None = Field(
        default=None, ge=1, description="First line of the finding (1-indexed)."
    )
    line_end: int | None = Field(
        default=None, ge=1, description="Last line of the finding (inclusive)."
    )
    snippet: str | None = Field(
        default=None, description="Relevant code snippet from the diff."
    )


# ---------------------------------------------------------------------------
# FSM phases
# ---------------------------------------------------------------------------


class EnumFsmPhase(StrEnum):
    INIT = "init"
    FETCH_DIFF = "fetch_diff"
    REVIEW = "review"
    POST_THREADS = "post_threads"
    WATCH = "watch"
    JUDGE_VERIFY = "judge_verify"
    REPORT = "report"
    DONE = "done"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Diff models
# ---------------------------------------------------------------------------


class DiffHunk(BaseModel):
    """A single contiguous segment of a unified diff for one file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str = Field(..., description="Repo-relative file path.")
    start_line: int = Field(
        ..., ge=1, description="First line of the hunk (1-indexed)."
    )
    end_line: int = Field(..., ge=1, description="Last line of the hunk (inclusive).")
    content: str = Field(..., description="Raw unified diff content for this hunk.")
    is_new_file: bool = Field(default=False)
    is_deleted_file: bool = Field(default=False)


# ---------------------------------------------------------------------------
# Input: ReviewRequest
# ---------------------------------------------------------------------------


class ReviewRequest(BaseModel):
    """Command payload to start a PR review bot run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Unique run identifier.")
    pr_number: int = Field(..., ge=1, description="GitHub PR number.")
    repo: str = Field(..., description="GitHub repo in owner/repo format.")
    reviewer_models: list[str] = Field(
        default_factory=lambda: ["qwen3-coder-30b", "qwen3-14b"],
        description="Reviewer model identifiers.",
    )
    judge_model: str = Field(
        default="deepseek-r1",
        description="Judge model identifier. Must not be a build-loop model.",
    )
    severity_threshold: EnumFindingSeverity = Field(
        default=EnumFindingSeverity.MAJOR,
        description="Minimum severity to post a review thread.",
    )
    dry_run: bool = Field(
        default=False, description="If true, post no GitHub comments."
    )
    max_findings_per_pr: int = Field(
        default=20,
        ge=1,
        description="Cap on review threads to prevent thread spam on large diffs.",
    )
    requested_at: datetime = Field(..., description="When the command was issued.")


# ---------------------------------------------------------------------------
# Review finding (PR bot extension of the shared ModelReviewFinding)
# ---------------------------------------------------------------------------


class ReviewFinding(BaseModel):
    """A single finding from the reviewer model, enriched with PR-specific context."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    id: UUID = Field(..., description="Unique finding identifier.")
    category: EnumFindingCategory = Field(...)
    severity: EnumFindingSeverity = Field(...)
    title: str = Field(..., min_length=1, max_length=120)
    description: str = Field(..., min_length=1, max_length=500)
    suggestion: str | None = Field(
        default=None,
        description="Optional concrete fix suggestion for the thread body.",
    )
    evidence: PrReviewFindingEvidence = Field(default_factory=PrReviewFindingEvidence)
    confidence: EnumReviewConfidence = Field(...)
    source_model: str = Field(..., min_length=1)

    @classmethod
    def from_model_review_finding(cls, finding: ModelReviewFinding) -> ReviewFinding:
        base_ev = finding.evidence
        pr_evidence = PrReviewFindingEvidence(
            file_path=base_ev.file_path if hasattr(base_ev, "file_path") else None,
        )
        return cls(
            id=finding.id,
            category=finding.category,
            severity=finding.severity,
            title=finding.title,
            description=finding.description,
            evidence=pr_evidence,
            confidence=finding.confidence,
            source_model=finding.source_model,
        )


# ---------------------------------------------------------------------------
# Thread state
# ---------------------------------------------------------------------------


class EnumThreadStatus(StrEnum):
    PENDING = "pending"
    POSTED = "posted"
    RESOLVED = "resolved"
    VERIFIED_PASS = "verified_pass"
    VERIFIED_FAIL = "verified_fail"
    ESCALATED = "escalated"


class ThreadState(BaseModel):
    """Tracks the lifecycle of a single GitHub review thread for one finding."""

    model_config = ConfigDict(frozen=False, extra="forbid")

    finding_id: UUID = Field(
        ..., description="ID of the ReviewFinding this thread covers."
    )
    github_thread_id: int | None = Field(
        default=None,
        description="GitHub pull request review comment ID, set after posting.",
    )
    status: EnumThreadStatus = Field(default=EnumThreadStatus.PENDING)
    posted_at: datetime | None = Field(default=None)
    resolved_at: datetime | None = Field(default=None)
    verified_at: datetime | None = Field(default=None)
    verify_attempts: int = Field(
        default=0,
        ge=0,
        description="Number of judge verification attempts for this thread.",
    )
    judge_reasoning: str | None = Field(
        default=None,
        description="Judge model reasoning for the most recent verdict.",
    )


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


class EnumPrVerdict(StrEnum):
    CLEAN = "clean"
    RISKS_NOTED = "risks_noted"
    BLOCKING_ISSUE = "blocking_issue"


class ReviewVerdict(BaseModel):
    """Aggregated result for a complete PR review bot run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Matches the ReviewRequest.correlation_id."
    )
    pr_number: int = Field(..., ge=1)
    repo: str = Field(...)
    verdict: EnumPrVerdict = Field(...)
    total_findings: int = Field(..., ge=0)
    threads_posted: int = Field(..., ge=0)
    threads_verified_pass: int = Field(..., ge=0)
    threads_verified_fail: int = Field(..., ge=0)
    threads_pending: int = Field(..., ge=0)
    judge_model_used: str = Field(...)
    duration_ms: int = Field(..., ge=0)
    completed_at: datetime = Field(...)
    summary: str = Field(
        default="", description="Human-readable summary posted as PR comment."
    )


__all__: list[str] = [
    "DiffHunk",
    "EnumFindingCategory",
    "EnumFindingSeverity",
    "EnumFsmPhase",
    "EnumPrVerdict",
    "EnumReviewConfidence",
    "EnumThreadStatus",
    "PrReviewFindingEvidence",
    "ReviewFinding",
    "ReviewRequest",
    "ReviewVerdict",
    "ThreadState",
]
