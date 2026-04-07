"""ModelReviewFinding — versioned structured finding schema for review workflows.

Canonical contract for review findings. All model responses are normalized into
this schema before aggregation or storage. See design doc section
"Structured Finding Schema" for the full specification.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumFindingCategory(StrEnum):
    SECURITY = "security"
    LOGIC_ERROR = "logic_error"
    INTEGRATION = "integration"
    SCOPE_VIOLATION = "scope_violation"
    CONTRACT_BREACH = "contract_breach"
    STYLE = "style"
    INFORMATIONAL = "informational"


class EnumFindingSeverity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"


class EnumReviewConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EnumReviewVerdict(StrEnum):
    CLEAN = "clean"
    RISKS_NOTED = "risks_noted"
    BLOCKING_ISSUE = "blocking_issue"


class ModelFindingEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str | None = Field(default=None)
    line_range: dict[str, int] | None = Field(default=None)
    code_snippet: str | None = Field(default=None)


class ModelReviewFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    id: UUID = Field(..., description="Unique finding identifier.")
    category: EnumFindingCategory = Field(...)
    severity: EnumFindingSeverity = Field(...)
    title: str = Field(..., min_length=1, max_length=120)
    description: str = Field(..., min_length=1, max_length=500)
    evidence: ModelFindingEvidence = Field(default_factory=ModelFindingEvidence)
    confidence: EnumReviewConfidence = Field(...)
    source_model: str = Field(..., min_length=1)
    detection_method: str = Field(default="")


__all__: list[str] = [
    "EnumFindingCategory",
    "EnumFindingSeverity",
    "EnumReviewConfidence",
    "EnumReviewVerdict",
    "ModelFindingEvidence",
    "ModelReviewFinding",
]
