# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pydantic models for per-attempt LLM dispatch tracing.

Each generation attempt in the delegation pipeline produces one
ModelDispatchTrace written to .onex_state/dispatch-traces/ and
optionally emitted to the event bus.

Related:
    - OMN-7855: Add dispatch tracing to .onex_state/
    - OMN-7856: Wire GLM-4.7-Flash as code reviewer
    - OMN-7857: Implement generate-review-retry loop
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelQualityGateResult(BaseModel):
    """Result of the structural quality gate (ruff + import + syntax checks)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ruff_pass: bool = Field(..., description="Whether ruff check passed.")
    import_pass: bool = Field(..., description="Whether import/syntax check passed.")
    test_pass: bool = Field(..., description="Whether any associated tests passed.")
    errors: list[str] = Field(
        default_factory=list,
        description="Error messages from failing checks.",
    )

    @property
    def all_pass(self) -> bool:
        """True if all three checks passed."""
        return self.ruff_pass and self.import_pass and self.test_pass


class ModelReviewIssue(BaseModel):
    """A single issue found during code review."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line: int | None = Field(default=None, description="Line number, if known.")
    severity: Literal["minor", "major", "critical"] = Field(
        ..., description="Issue severity."
    )
    message: str = Field(..., description="Issue description.")


class ModelReviewResult(BaseModel):
    """Structured output from the GLM-4.7-Flash code reviewer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approved: bool = Field(..., description="Whether the code is approved.")
    issues: list[ModelReviewIssue] = Field(
        default_factory=list, description="Issues found."
    )
    risk_level: Literal["low", "medium", "high"] = Field(
        default="low", description="Overall risk level."
    )
    reviewer_model: str = Field(default="", description="Model ID used for review.")
    review_tokens: int = Field(
        default=0,
        ge=0,
        description="Tokens consumed by the review call.",
    )


class ModelDispatchTrace(BaseModel):
    """Trace record for a single LLM generation attempt.

    Written to .onex_state/dispatch-traces/{correlation_id}-{ticket_id}-attempt-{N}.json
    after every attempt (pass or fail). Local files are authoritative.
    Bus events (onex.evt.omnimarket.delegation-attempt.v1) are observability copies.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., description="Root correlation ID for the run.")
    ticket_id: str = Field(..., description="Linear ticket identifier.")
    attempt: int = Field(..., ge=1, description="Attempt number (1-indexed).")
    timestamp: str = Field(..., description="ISO-8601 UTC timestamp of this attempt.")
    coder_model: str = Field(..., description="Model ID used for code generation.")
    reviewer_model: str | None = Field(
        default=None,
        description="Model ID used for review (None if review was skipped).",
    )
    prompt_tokens: int = Field(default=0, ge=0, description="Tokens in the prompt.")
    completion_tokens: int = Field(
        default=0, ge=0, description="Tokens in the completion."
    )
    prompt_chars: int = Field(
        default=0,
        ge=0,
        description="Character count of prompt (for context limit debugging).",
    )
    generation_raw: str = Field(
        default="",
        description="Full raw model response (before code extraction).",
    )
    quality_gate: ModelQualityGateResult = Field(
        ..., description="Structural quality gate results."
    )
    review_result: ModelReviewResult | None = Field(
        default=None,
        description="Reviewer result (None if review was not run).",
    )
    accepted: bool = Field(
        ...,
        description=(
            "True if this attempt passed structural gate and was not rejected "
            "under current review policy."
        ),
    )
    wall_clock_ms: int = Field(
        default=0,
        ge=0,
        description="Wall-clock time for this attempt in milliseconds.",
    )
    failure_kind: str | None = Field(
        default=None,
        description=(
            "Failure taxonomy: generation_malformed, gate_failed, review_rejected, "
            "review_unavailable, transport_failure. None if accepted."
        ),
    )


__all__: list[str] = [
    "ModelDispatchTrace",
    "ModelQualityGateResult",
    "ModelReviewIssue",
    "ModelReviewResult",
]
