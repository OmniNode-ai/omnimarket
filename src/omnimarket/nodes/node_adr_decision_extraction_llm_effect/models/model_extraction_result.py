# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelExtractionResult -- output contract for node_adr_decision_extraction_llm_effect."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumDecisionType(StrEnum):
    """Six-type taxonomy for architectural decisions and related content."""

    architecture_decision = "architecture_decision"
    architecture_pivot = "architecture_pivot"
    doctrine_formation = "doctrine_formation"
    operational_lesson = "operational_lesson"
    supersession = "supersession"
    rejected_approach = "rejected_approach"


class ModelDecisionExtraction(BaseModel):
    """A single extracted decision or related item from a document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    extraction_id: str = Field(
        ...,
        description=(
            "Deterministic ID: sha256(extraction_version + model_id + "
            "sorted(source_segment_ids) + sorted(segment_content_hashes))."
        ),
    )
    decision_type: EnumDecisionType = Field(
        ...,
        description="Classification of this extraction within the six-type taxonomy.",
    )
    statement: str = Field(
        ...,
        description="Concise statement of the decision, pivot, lesson, or doctrine.",
    )
    rationale: str | None = Field(
        default=None,
        description="Supporting rationale or context from the source text.",
    )
    source_segment_ids: list[str] = Field(
        ...,
        description="Segment IDs that contributed to this extraction.",
        min_length=1,
    )
    evidence_quotes: list[str] = Field(
        default_factory=list,
        description="Verbatim quotes from source segments supporting this extraction.",
    )
    extraction_model_id: str = Field(
        ...,
        description="Exact model identifier used to produce this extraction.",
    )
    prompt_template_id: str = Field(
        ...,
        description="Identifier for the extraction prompt template.",
    )
    prompt_template_version: str = Field(
        ...,
        description="Semantic version of the extraction prompt template.",
    )
    confidence: float = Field(
        ...,
        description="Model's self-reported confidence in this extraction (0.0-1.0).",
        ge=0.0,
        le=1.0,
    )


class ModelLLMCallEvidence(BaseModel):
    """Evidence record for a single LLM call made during extraction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_template_id: str = Field(
        ..., description="Identifier for the prompt template used."
    )
    prompt_template_version: str = Field(
        ..., description="Semantic version of the prompt template."
    )
    extraction_model_key: str = Field(
        ..., description="Model key used for extraction (e.g. 'qwen3-coder')."
    )
    extraction_model_id: str = Field(
        ..., description="Exact model ID sent in the API request."
    )
    prompt_tokens: int = Field(default=0, description="Prompt token count.", ge=0)
    completion_tokens: int = Field(
        default=0, description="Completion token count.", ge=0
    )
    latency_ms: int = Field(
        default=0, description="Wall-clock latency in milliseconds.", ge=0
    )
    json_repair_attempted: bool = Field(
        default=False,
        description="True when a JSON repair re-prompt was issued.",
    )


class ModelExtractionResult(BaseModel):
    """Result of a single ADR decision extraction call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(
        ..., description="Correlation ID from the parent canary run."
    )
    source_path: str = Field(..., description="Source document path.")
    model_key: str = Field(..., description="Short model key used for this extraction.")
    success: bool = Field(
        ...,
        description="True when extraction completed; false on LLM or parse failure.",
    )

    extractions: list[ModelDecisionExtraction] = Field(
        default_factory=list,
        description="Extracted decisions — populated on success.",
    )

    # Failure details — only populated when success=False
    error_code: str | None = Field(
        default=None, description="Machine-readable error code."
    )
    error_message: str | None = Field(
        default=None, description="Human-readable error message."
    )
    model_id: str | None = Field(
        default=None,
        description="Model ID that failed (set on LLM call failure).",
    )
    retryable: bool = Field(
        default=False,
        description="Whether the failure is likely transient and worth retrying.",
    )

    llm_call_evidence: ModelLLMCallEvidence | None = Field(
        default=None,
        description="Evidence record for the LLM extraction call.",
    )


__all__: list[str] = [
    "EnumDecisionType",
    "ModelDecisionExtraction",
    "ModelExtractionResult",
    "ModelLLMCallEvidence",
]
