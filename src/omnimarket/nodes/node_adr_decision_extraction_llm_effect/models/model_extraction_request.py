# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelExtractionRequest -- input contract for node_adr_decision_extraction_llm_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDocumentSegment(BaseModel):
    """A single semantic unit from the upstream segmentation node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    segment_id: str = Field(
        ..., description="Deterministic segment identifier (sha256)."
    )
    source_path: str = Field(..., description="Relative path of the source document.")
    start_line: int = Field(
        ..., description="1-based start line in source document.", ge=1
    )
    end_line: int = Field(..., description="1-based end line (inclusive).", ge=1)
    segment_type: str = Field(
        ..., description="Semantic classification of this segment."
    )
    content: str = Field(..., description="Verbatim text of this segment.")
    confidence: float = Field(
        ...,
        description="Classification confidence (0.0-1.0).",
        ge=0.0,
        le=1.0,
    )


class ModelExtractionRequest(BaseModel):
    """Command payload for a single ADR decision extraction call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    segments: list[ModelDocumentSegment] = Field(
        ...,
        description="Segmented document content from upstream segmentation node.",
        min_length=1,
    )
    model_key: str = Field(
        ...,
        description="Short key for the LLM to use (resolved via AdapterInferenceBridge).",
    )
    model_config_overrides: dict[str, object] = Field(
        default_factory=dict,
        description="Optional per-call overrides for the inference bridge config entry.",
    )
    correlation_id: str = Field(
        ...,
        description="Correlation ID linking this extraction to the parent canary run.",
    )
    source_path: str = Field(
        ...,
        description="Source document path (for tracing and deduplication).",
    )


__all__: list[str] = ["ModelDocumentSegment", "ModelExtractionRequest"]
