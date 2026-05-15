# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared delegation event payload models."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_budget_policy_compute.models.model_budget_limits import (
    ModelBudgetLimits,
)

EnumQualityContractMode = Literal["extend_task_class", "replace_task_class"]

SUPPORTED_ACCEPTANCE_CRITERIA = frozenset(
    {
        "exactly_two_sentences",
        "plain_text_only",
        "response_non_empty",
    }
)
MAX_WORDS_PER_SENTENCE_RE = re.compile(r"^max_words_per_sentence_([1-9]\d*)$")


def validate_acceptance_criteria(criteria: tuple[str, ...]) -> tuple[str, ...]:
    """Validate request-level quality criteria before they enter dispatch."""
    unsupported = [
        item
        for item in criteria
        if item not in SUPPORTED_ACCEPTANCE_CRITERIA
        and not MAX_WORDS_PER_SENTENCE_RE.match(item)
    ]
    if unsupported:
        joined = ", ".join(sorted(unsupported))
        raise ValueError(f"unsupported acceptance criteria: {joined}")
    return criteria


class ModelDelegationRequest(BaseModel):
    """Delegation command: prompt, task type, and source context."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    prompt: str = Field(
        ...,
        description="The user prompt to delegate to the local LLM.",
    )
    task_type: Literal["test", "document", "research"] = Field(
        ...,
        description="Classification of the delegation task.",
    )
    source_session_id: str | None = Field(
        default=None,
        description="Session that originated the delegation request.",
    )
    source_file_path: str | None = Field(
        default=None,
        description="File context for the delegation, if any.",
    )
    correlation_id: UUID = Field(
        ...,
        description="Unique identifier for tracking through the pipeline.",
    )
    max_tokens: int = Field(
        default=2048,
        description="Maximum tokens for the LLM response.",
    )
    emitted_at: datetime = Field(
        ...,
        description="Timestamp when the request was created.",
    )
    output_schema_key: str | None = Field(
        default=None,
        description=(
            "When set, the orchestrator runs the schema-compliance loop: it "
            "validates each inference response against the registry-resolved "
            "schema and emits repair prompts on validation failure. None = "
            "legacy single-attempt path."
        ),
    )
    compliance_budget: ModelBudgetLimits | None = Field(
        default=None,
        description=(
            "Budget ceilings (tokens, cost, elapsed time) the compliance loop "
            "enforces between repair attempts. Required when "
            "``output_schema_key`` is set."
        ),
    )
    quality_contract_mode: EnumQualityContractMode = Field(
        default="extend_task_class",
        description=(
            "How request-level acceptance criteria interact with task-class DoD."
        ),
    )
    acceptance_criteria: tuple[str, ...] = Field(
        default=(),
        description="Request-level quality checks enforced by the quality gate.",
    )

    @model_validator(mode="after")
    def _validate_compliance_loop_config(self) -> Self:
        """Reject ``output_schema_key`` set without ``compliance_budget``."""
        if self.output_schema_key is not None and self.compliance_budget is None:
            msg = (
                "compliance_budget is required when output_schema_key is set "
                "(the compliance loop has nothing to evaluate against without "
                "token / cost / time ceilings)"
            )
            raise ValueError(msg)
        validate_acceptance_criteria(self.acceptance_criteria)
        return self


class ModelDelegationResult(BaseModel):
    """Delegation outcome: content, quality status, model info, and metrics."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID = Field(
        ...,
        description="Tracks this result back to the original request.",
    )
    task_type: str = Field(
        ...,
        description="The task classification from the original request.",
    )
    model_used: str = Field(
        ...,
        description="Name of the LLM model that produced the response.",
    )
    endpoint_url: str = Field(
        ...,
        description="URL of the LLM endpoint used.",
    )
    content: str = Field(
        ...,
        description="The LLM-generated response content.",
    )
    quality_passed: bool = Field(
        ...,
        description="Whether the quality gate accepted the response.",
    )
    quality_score: float = Field(
        ...,
        description="Quality score from 0.0 to 1.0.",
    )
    latency_ms: int = Field(
        ...,
        description="End-to-end latency in milliseconds.",
    )
    prompt_tokens: int = Field(
        default=0,
        description="Number of tokens in the prompt.",
    )
    completion_tokens: int = Field(
        default=0,
        description="Number of tokens in the completion.",
    )
    total_tokens: int = Field(
        default=0,
        description="Total tokens used (prompt + completion).",
    )
    fallback_to_claude: bool = Field(
        ...,
        description="Whether fallback to Claude was triggered.",
    )
    failure_reason: str = Field(
        default="",
        description="Reason for failure, empty string if successful.",
    )
    tokens_to_compliance: int = Field(
        default=0,
        ge=0,
        description="Total tokens across all compliance attempts.",
    )
    compliance_attempts: int = Field(
        default=1,
        ge=1,
        description="Number of LLM invocations to reach compliance.",
    )


__all__ = [
    "MAX_WORDS_PER_SENTENCE_RE",
    "SUPPORTED_ACCEPTANCE_CRITERIA",
    "EnumQualityContractMode",
    "ModelDelegationRequest",
    "ModelDelegationResult",
    "validate_acceptance_criteria",
]
