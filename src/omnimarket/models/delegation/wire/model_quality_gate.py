# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Delegation quality gate wire DTOs."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.wire.model_delegation_request import (
    EnumQualityContractMode,
)

EnumQualityGateCategory = Literal["pass", "fail_deterministic", "fail_heuristic"]


class ModelQualityGateInput(BaseModel):
    """Gate input: LLM response content and expected quality markers."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID = Field(
        ...,
        description="Tracks this input back to the original request.",
    )
    task_type: str = Field(
        ...,
        description="The task classification for type-specific checks.",
    )
    llm_response_content: str = Field(
        ...,
        description="The raw LLM response to evaluate.",
    )
    expected_markers: tuple[str, ...] = Field(
        default=(),
        description="Strings expected in the response for the task type.",
    )
    min_response_length: int = Field(
        default=60,
        description="Minimum acceptable response length in characters.",
    )
    dod_deterministic: tuple[str, ...] = Field(
        default=(),
        description=(
            "Deterministic DoD check names from the task-class contract (OMN-10614). "
            "These checks BLOCK delegation result injection on failure."
        ),
    )
    dod_heuristic: tuple[str, ...] = Field(
        default=(),
        description=(
            "Heuristic DoD check names from the task-class contract (OMN-10614). "
            "These checks escalate per contract policy on failure."
        ),
    )
    quality_contract_mode: EnumQualityContractMode = Field(
        default="extend_task_class",
        description="How request-level acceptance criteria interact with task-class DoD.",
    )
    acceptance_criteria: tuple[str, ...] = Field(
        default=(),
        description="Request-level quality checks enforced by the quality gate.",
    )


class ModelQualityGateResult(BaseModel):
    """Gate output: pass/fail verdict, score, failure reasons, and fallback flag."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        from_attributes=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    correlation_id: UUID = Field(
        ...,
        description="Tracks this result back to the original request.",
    )
    passed: bool = Field(
        ..., description="Whether the LLM response passed the quality gate."
    )
    fail_category: EnumQualityGateCategory = Field(
        default="pass",
        description=(
            "Structured outcome: 'pass', 'fail_deterministic' (hard block), "
            "or 'fail_heuristic' (escalate per contract policy)."
        ),
    )
    quality_score: float = Field(..., description="Quality score from 0.0 to 1.0.")
    failure_reasons: tuple[str, ...] = Field(
        default=(),
        description="Tuple of human-readable failure reason strings.",
    )
    fallback_recommended: bool = Field(
        default=False,
        description="Whether fallback to Claude is recommended.",
    )
    score_source: str = Field(
        default="",
        description="Authority that produced actual_score when applicable.",
    )
    acceptance_version: str = Field(
        default="",
        description="Deterministic acceptance evaluator version when applicable.",
    )
    corpus_hash: str = Field(
        default="",
        description="Stable hash of the deterministic acceptance corpus/check set.",
    )
    validator_or_artifact_hash: str = Field(
        default="",
        description="Stable hash of the validator or delegated artifact evaluated.",
    )
    acceptance_command: str = Field(
        default="",
        description="Replay command or command identity for deterministic acceptance.",
    )
    actual_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Authority score produced by the acceptance source.",
    )
    pass_: bool | None = Field(
        default=None,
        alias="pass",
        description="Authority pass/fail verdict produced by the acceptance source.",
    )
    failure_cases: tuple[str, ...] = Field(
        default=(),
        description="Deterministic acceptance failure cases.",
    )


__all__: list[str] = [
    "EnumQualityGateCategory",
    "ModelQualityGateInput",
    "ModelQualityGateResult",
]
