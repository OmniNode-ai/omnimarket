# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared node-I/O models for the review node family (OMN-13210 / B1).

OWNER module for the request/result models exchanged between
node_hostile_reviewer_orchestrator and the review COMPUTE/EFFECT nodes
(node_review_prompt_builder_compute, node_review_response_parser_compute,
node_github_diff_effect, node_review_convergence_compute).

Living in the shared ``omnimarket.review`` package means the orchestrator and
each node import these from one owner — no node reaches into a sibling node's
private ``models`` package (omnimarket CLAUDE.md "promote shared types instead";
enforced by tests/test_no_cross_node_reach_in.py).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_review_finding import EnumFindingCategory
from omnimarket.review.prompt_builder import ModelPromptBuilderOutput
from omnimarket.review.response_parser import ModelParseResult

# ---------------------------------------------------------------------------
# Prompt builder COMPUTE I/O
# ---------------------------------------------------------------------------


class ModelReviewPromptBuilderRequest(BaseModel):
    """Request to build an adversarial-review prompt for one model route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Review run correlation ID.")
    prompt_template_id: str = Field(
        default="adversarial_reviewer_pr",
        description="One of: adversarial_reviewer_pr, adversarial_reviewer_plan.",
    )
    context_content: str = Field(..., description="The diff or plan content.")
    model_context_window: int = Field(
        ..., ge=1024, description="Target model context window in tokens."
    )
    persona_markdown: str | None = Field(
        default=None,
        description="Optional persona tone directive prepended to system prompt.",
    )


# ---------------------------------------------------------------------------
# Response parser COMPUTE I/O
# ---------------------------------------------------------------------------


class ModelReviewResponseParserRequest(BaseModel):
    """Request to parse one model's raw review response into findings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Review run correlation ID.")
    raw_text: str = Field(..., description="Raw LLM response text to parse.")
    source_model: str = Field(
        ..., min_length=1, description="Logical model route key that produced the text."
    )


# ---------------------------------------------------------------------------
# GitHub diff EFFECT I/O
# ---------------------------------------------------------------------------


class ModelGithubDiffCommand(BaseModel):
    """Command to resolve a review target's content.

    Exactly one of (``pr_number`` + ``repo``) or ``file_path`` must be supplied.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Review run correlation ID.")
    repo: str | None = Field(
        default=None, description="GitHub repo slug 'owner/name' (with pr_number)."
    )
    pr_number: int | None = Field(
        default=None, description="Pull request number to resolve the diff for."
    )
    file_path: str | None = Field(
        default=None, description="Local file path to review (alternative to PR)."
    )

    @model_validator(mode="after")
    def _validate_target(self) -> ModelGithubDiffCommand:
        has_pr = self.pr_number is not None
        has_file = self.file_path is not None
        if has_pr == has_file:
            raise ValueError(
                "supply exactly one review target: (pr_number + repo) OR file_path"
            )
        if has_pr and not self.repo:
            raise ValueError("repo is required when pr_number is supplied")
        return self


class ModelGithubDiffResolvedEvent(BaseModel):
    """Event carrying the resolved review-target content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    repo: str | None = Field(default=None)
    pr_number: int | None = Field(default=None)
    file_path: str | None = Field(default=None)
    content: str = Field(..., description="Resolved unified diff or file content.")
    content_chars: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# Convergence COMPUTE I/O (eval tooling)
# ---------------------------------------------------------------------------


class ModelFindingLabel(BaseModel):
    """A single finding labeled for local-vs-frontier agreement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: UUID = Field(...)
    category: EnumFindingCategory = Field(...)
    local_detected: bool = Field(...)
    frontier_detected: bool = Field(...)


class ModelConvergenceInput(BaseModel):
    """Labeled findings for one model route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Review run correlation ID.")
    model_key: str = Field(..., min_length=1)
    labels: list[ModelFindingLabel] = Field(default_factory=list)


class ModelConvergenceOutput(BaseModel):
    """Per-model precision / recall / F1 against frontier ground truth."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(...)
    overall_f1: float = Field(default=0.0)
    overall_precision: float = Field(default=0.0)
    overall_recall: float = Field(default=0.0)
    by_category: dict[str, float] = Field(default_factory=dict)
    true_positives: int = Field(default=0)
    false_positives: int = Field(default=0)
    false_negatives: int = Field(default=0)
    total_labels: int = Field(default=0)


__all__: list[str] = [
    "ModelConvergenceInput",
    "ModelConvergenceOutput",
    "ModelFindingLabel",
    "ModelGithubDiffCommand",
    "ModelGithubDiffResolvedEvent",
    "ModelParseResult",
    "ModelPromptBuilderOutput",
    "ModelReviewPromptBuilderRequest",
    "ModelReviewResponseParserRequest",
]
