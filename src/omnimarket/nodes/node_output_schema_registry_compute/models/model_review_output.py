"""ModelReviewOutput — structured output schema for LLM review results."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumReviewVerdict(StrEnum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    COMMENT = "comment"


class ModelReviewFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: str
    category: str
    description: str
    file_path: str | None = None
    line_number: int | None = None
    suggestion: str | None = None


class ModelReviewOutput(BaseModel):
    """Structured output produced by an LLM code-review pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumReviewVerdict
    summary: str
    findings: list[ModelReviewFinding] = []
    confidence: float = 1.0
    model_id: str = ""
    run_id: str = ""


__all__ = [
    "EnumReviewVerdict",
    "ModelReviewFinding",
    "ModelReviewOutput",
]
