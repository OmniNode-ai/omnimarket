# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The readable record of one quality-gate decision.

The bus orchestrator emits ``ModelQualityGateResult`` on
``onex.evt.omnibase-infra.quality-gate-result.v1``. The in-process dispatch
port decides in process, so its decision used to leave only a pass flag and a
score on the attempt record. This record names what the decision rested on:
the checks evaluated, the checks skipped for want of evidence, the checks
failed and the rule that decided. It reuses the field names, the category
vocabulary and the per-rule verdict type of the bus result, so both paths
describe a decision in one shape.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.wire.model_quality_gate import (
    EnumQualityGateCategory,
    ModelQualityRuleEvaluation,
)


def quality_gate_record_key(correlation_id: UUID, attempt_index: int) -> str:
    """The key one gate decision is recorded under: run plus attempt position."""
    return f"{correlation_id}:{attempt_index}"


class ModelQualityGateRecord(BaseModel):
    """What one gate decision evaluated, skipped and failed, and what decided."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_key: str = Field(
        ...,
        min_length=1,
        description="Correlation id and attempt index; a rerun of one attempt repeats it.",
    )
    correlation_id: UUID = Field(..., description="The run this decision belongs to.")
    attempt_index: int = Field(
        ..., ge=0, description="Position of the attempt in the run's ladder."
    )
    task_type: str = Field(..., description="The task class the gate judged.")
    passed: bool = Field(..., description="The gate's verdict on the response.")
    fail_category: EnumQualityGateCategory = Field(
        ..., description="Structured outcome, as the bus result names it."
    )
    quality_score: float = Field(..., description="Quality score from 0.0 to 1.0.")
    checks_evaluated: tuple[ModelQualityRuleEvaluation, ...] = Field(
        default=(), description="Each check that ran, with its own verdict."
    )
    checks_skipped: tuple[str, ...] = Field(
        default=(),
        description="Declared checks not evaluated because their evidence was not supplied.",
    )
    checks_failed: tuple[str, ...] = Field(
        default=(), description="Names of the evaluated checks that did not pass."
    )
    deciding_rule: str = Field(
        ...,
        min_length=1,
        description="The typed reason the attempt was kept or abandoned.",
    )
    acceptance_detail: str = Field(
        default="", description="Human-readable detail behind the deciding rule."
    )
    failure_reasons: tuple[str, ...] = Field(
        default=(), description="Failure reasons, as the bus result carries them."
    )
    fallback_recommended: bool = Field(
        default=False, description="Whether the gate recommended climbing."
    )
    score_source: str = Field(
        default="", description="Authority that produced the score, when applicable."
    )


__all__ = ["ModelQualityGateRecord", "quality_gate_record_key"]
