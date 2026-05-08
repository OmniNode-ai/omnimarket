# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelGradingResult -- output contract for node_adr_extraction_grader_llm_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLLMCallEvidence(BaseModel):
    """Evidence record for a single LLM call made during grading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_template_id: str = Field(
        ...,
        description="Identifier for the prompt template used.",
    )
    prompt_template_version: str = Field(
        ...,
        description="Semantic version of the prompt template.",
    )
    grader_model_key: str = Field(
        ...,
        description="Model key used for grading (e.g. 'opus').",
    )
    prompt_tokens: int = Field(
        default=0,
        description="Prompt token count from the grading call.",
        ge=0,
    )
    completion_tokens: int = Field(
        default=0,
        description="Completion token count from the grading call.",
        ge=0,
    )
    latency_ms: int = Field(
        default=0,
        description="Wall-clock latency in milliseconds.",
        ge=0,
    )


class ModelGradingResult(BaseModel):
    """Result of a single ADR extraction grading call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(
        ...,
        description="Correlation ID from the parent canary run.",
    )
    model_key_under_test: str = Field(
        ...,
        description="Short key identifying the extraction model that was graded.",
    )
    success: bool = Field(
        ...,
        description="True when grading completed and scores are valid; false on grader failure.",
    )

    # Scores — only populated when success=True
    recall: float | None = Field(
        default=None,
        description="Fraction of ground-truth decisions captured in extraction output (0.0-1.0).",
        ge=0.0,
        le=1.0,
    )
    precision: float | None = Field(
        default=None,
        description="Fraction of extracted decisions that are accurate vs. the ground truth (0.0-1.0).",
        ge=0.0,
        le=1.0,
    )
    fidelity: float | None = Field(
        default=None,
        description="Semantic faithfulness of the extracted content to the source (0.0-1.0).",
        ge=0.0,
        le=1.0,
    )
    format_compliance: float | None = Field(
        default=None,
        description="Adherence to expected extraction output schema/format (0.0-1.0).",
        ge=0.0,
        le=1.0,
    )

    # Failure details — only populated when success=False
    error_code: str | None = Field(
        default=None,
        description="Machine-readable error code when grading failed.",
    )
    error_message: str | None = Field(
        default=None,
        description="Human-readable error message when grading failed.",
    )

    llm_call_evidence: ModelLLMCallEvidence | None = Field(
        default=None,
        description="Evidence record for the LLM grading call; populated on success.",
    )


__all__: list[str] = ["ModelGradingResult", "ModelLLMCallEvidence"]
