# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the delegated-answer acceptance gate (OMN-20489)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.wire.model_answer_gate_request import (
    EnumAnswerGateKind,
    ModelIndependentJudgeVerdict,
)


class ModelAnswerGateResult(BaseModel):
    """The acceptance decision and both verdicts it was made from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    task_type: str
    gate_kind: EnumAnswerGateKind
    accepted: bool
    contract_passed: bool
    judge: ModelIndependentJudgeVerdict | None = None
    producer_family: str | None = None
    judge_family: str | None = None
    judge_independent: bool | None = Field(
        default=None,
        description="Whether the judge's model family differs from the producer's; None when no judge applies.",
    )
    tests_passed: bool | None = Field(
        default=None,
        description="Whether the named tests ran and passed; None when no test run applies.",
    )
    rejection_reasons: tuple[str, ...] = ()
    fallback_recommended: bool = False


__all__ = ["ModelAnswerGateResult"]
