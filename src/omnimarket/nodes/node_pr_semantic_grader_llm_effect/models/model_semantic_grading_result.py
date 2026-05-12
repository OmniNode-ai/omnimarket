# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelSemanticGradingResult -- output contract for node_pr_semantic_grader_llm_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_ANTI_PATTERN_ADVISORY_THRESHOLD = 0.7


class ModelLLMCallEvidence(BaseModel):
    """Evidence record for a single LLM call made during semantic grading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_template_id: str = Field(
        ..., description="Identifier for the prompt template used."
    )
    prompt_template_version: str = Field(
        ..., description="Semantic version of the prompt template."
    )
    grader_model_key: str = Field(
        ..., description="Model key used for grading (e.g. 'opus')."
    )
    prompt_tokens: int = Field(default=0, description="Prompt token count.", ge=0)
    completion_tokens: int = Field(
        default=0, description="Completion token count.", ge=0
    )
    latency_ms: int = Field(
        default=0, description="Wall-clock latency in milliseconds.", ge=0
    )


class ModelSemanticGradingResult(BaseModel):
    """Result of a single PR semantic grading call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(
        ...,
        description="Correlation ID from the triggering event.",
    )
    ticket_id: str = Field(
        ...,
        description="Ticket identifier that was graded.",
    )
    success: bool = Field(
        ...,
        description="True when grading completed and scores are valid; false on grader failure.",
    )

    # Scores — only populated when success=True
    criteria_coverage: float | None = Field(
        default=None,
        description="Fraction of acceptance criteria addressed by the diff (0.0-1.0).",
        ge=0.0,
        le=1.0,
    )
    contract_alignment: float | None = Field(
        default=None,
        description="Degree to which the implementation follows declared contracts, not hardcoded values (0.0-1.0).",
        ge=0.0,
        le=1.0,
    )
    anti_pattern_present: float | None = Field(
        default=None,
        description="Presence of forbidden patterns (0.0=clean, 1.0=severe violations).",
        ge=0.0,
        le=1.0,
    )
    overall_confidence: float | None = Field(
        default=None,
        description="Grader's confidence in the assessment (0.0-1.0).",
        ge=0.0,
        le=1.0,
    )
    rationale: str | None = Field(
        default=None,
        description="One sentence per dimension separated by '|'.",
    )

    # Advisory flag — True when anti_pattern_present >= 0.7 (Phase 1 threshold)
    advisory: bool = Field(
        default=False,
        description="True when anti_pattern_present >= 0.7; Phase 1 advisory flag.",
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

    @classmethod
    def with_scores(
        cls,
        *,
        correlation_id: str,
        ticket_id: str,
        criteria_coverage: float,
        contract_alignment: float,
        anti_pattern_present: float,
        overall_confidence: float,
        rationale: str | None,
        llm_call_evidence: ModelLLMCallEvidence | None,
    ) -> ModelSemanticGradingResult:
        return cls(
            correlation_id=correlation_id,
            ticket_id=ticket_id,
            success=True,
            criteria_coverage=criteria_coverage,
            contract_alignment=contract_alignment,
            anti_pattern_present=anti_pattern_present,
            overall_confidence=overall_confidence,
            rationale=rationale,
            advisory=anti_pattern_present >= _ANTI_PATTERN_ADVISORY_THRESHOLD,
            llm_call_evidence=llm_call_evidence,
        )


__all__: list[str] = ["ModelLLMCallEvidence", "ModelSemanticGradingResult"]
