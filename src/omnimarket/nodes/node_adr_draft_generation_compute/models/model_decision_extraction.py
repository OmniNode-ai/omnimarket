"""Local stand-in for ModelDecisionExtraction pending OMN-10691 (omnibase_core)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumDecisionType(StrEnum):
    ARCHITECTURE = "ARCHITECTURE"
    TECHNOLOGY = "TECHNOLOGY"
    PROCESS = "PROCESS"
    SECURITY = "SECURITY"
    DATA = "DATA"
    INTEGRATION = "INTEGRATION"


class ModelExtractionProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_doc_paths: list[str] = Field(default_factory=list)
    prompt_template_id: str = ""
    prompt_template_version: str = ""
    pipeline_version: str = ""
    timestamp: str = ""


class ModelDecisionExtraction(BaseModel):
    """Local stand-in model until omnibase_core ships OMN-10691 ADR domain models."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    extraction_id: str
    title: str
    decision_type: EnumDecisionType
    rationale_bullets: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)
    alternatives_considered: list[str] = Field(default_factory=list)
    model_id: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    provenance: ModelExtractionProvenance = Field(
        default_factory=ModelExtractionProvenance
    )
    canary_run_id: str = ""


__all__ = [
    "EnumDecisionType",
    "ModelDecisionExtraction",
    "ModelExtractionProvenance",
]
