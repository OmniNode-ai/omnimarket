# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelSegmentationResult -- output contract for node_adr_segmentation_llm_effect."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256_HEX_RE = r"^[0-9a-f]{64}$"


class EnumSegmentType(StrEnum):
    decision = "decision"
    critique = "critique"
    proposal = "proposal"
    migration = "migration"
    invariant = "invariant"
    failure_analysis = "failure_analysis"
    operational_concern = "operational_concern"
    hypothesis = "hypothesis"
    doctrine_formation = "doctrine_formation"
    implementation_detail = "implementation_detail"
    architectural_risk = "architectural_risk"
    non_decision = "non_decision"
    background = "background"
    unknown = "unknown"


class ModelDocumentSegment(BaseModel):
    """A single semantic unit extracted from a source document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    segment_id: str = Field(
        ...,
        pattern=_SHA256_HEX_RE,
        description=(
            "Deterministic SHA-256 hex digest of "
            "source_path + source_content_sha256 + start_line + end_line + segment_type."
        ),
    )
    source_path: str = Field(
        ..., description="Repository-relative path to the source document."
    )
    source_content_sha256: str = Field(
        ...,
        pattern=_SHA256_HEX_RE,
        description="64-hex SHA-256 digest of the full source document content.",
    )
    start_line: int = Field(
        ..., description="1-based start line of this segment.", ge=1
    )
    end_line: int = Field(
        ..., description="1-based end line of this segment (inclusive).", ge=1
    )
    segment_type: EnumSegmentType = Field(
        ..., description="Semantic classification of this segment."
    )
    content: str = Field(..., description="Verbatim text of this segment.")
    segment_content_sha256: str = Field(
        ...,
        pattern=_SHA256_HEX_RE,
        description="64-hex SHA-256 digest of the segment content.",
    )
    confidence: float = Field(
        ...,
        description="LLM-reported confidence for the classification (0.0-1.0).",
        ge=0.0,
        le=1.0,
    )

    @model_validator(mode="after")
    def validate_line_span(self) -> ModelDocumentSegment:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ModelLLMCallEvidence(BaseModel):
    """Evidence record for the LLM call made during segmentation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_template_id: str = Field(
        ..., description="Identifier for the prompt template used."
    )
    prompt_template_version: str = Field(
        ..., description="Semantic version of the prompt template."
    )
    model_key: str = Field(..., description="Model key used for segmentation.")
    prompt_hash: str = Field(
        ...,
        pattern=_SHA256_HEX_RE,
        description="64-hex SHA-256 digest of the full prompt sent.",
    )
    input_hash: str = Field(
        ...,
        pattern=_SHA256_HEX_RE,
        description="64-hex SHA-256 digest of the source document content.",
    )
    response_hash: str = Field(
        ...,
        pattern=_SHA256_HEX_RE,
        description="64-hex SHA-256 digest of the raw LLM response.",
    )
    usage_source: str = Field(
        default="llm_response",
        description="Source of token count data (e.g. 'llm_response', 'estimated').",
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
        description="True if a JSON repair re-prompt was attempted.",
    )


class ModelSegmentationResult(BaseModel):
    """Result of a single document segmentation call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(
        ..., description="Correlation ID from the parent pipeline run."
    )
    source_path: str = Field(
        ..., description="Repository-relative path to the source document."
    )
    success: bool = Field(
        ...,
        description="True when segmentation completed and segments are valid; false on failure.",
    )

    segments: list[ModelDocumentSegment] = Field(
        default_factory=list,
        description="Extracted semantic segments; populated on success.",
    )

    error_code: str | None = Field(
        default=None,
        description="Machine-readable error code when segmentation failed.",
    )
    error_message: str | None = Field(
        default=None,
        description="Human-readable error message when segmentation failed.",
    )
    retryable: bool = Field(
        default=False,
        description="True if the failure is transient and a retry may succeed.",
    )
    model_id: str | None = Field(
        default=None,
        description="Resolved model identifier used for this call.",
    )

    llm_call_evidence: ModelLLMCallEvidence | None = Field(
        default=None,
        description="Evidence record for the LLM segmentation call.",
    )

    @model_validator(mode="after")
    def validate_success_failure_shape(self) -> ModelSegmentationResult:
        if self.success:
            if self.error_code is not None or self.error_message is not None:
                raise ValueError(
                    "error_code and error_message must be unset when success=True"
                )
            return self

        if not self.error_code or not self.error_message or self.model_id is None:
            raise ValueError(
                "error_code, error_message, and model_id are required when success=False"
            )
        return self


__all__: list[str] = [
    "EnumSegmentType",
    "ModelDocumentSegment",
    "ModelLLMCallEvidence",
    "ModelSegmentationResult",
]
