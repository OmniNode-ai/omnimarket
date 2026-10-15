# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared input and judge evidence for delegated-answer acceptance (OMN-20489)."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult


class EnumAnswerGateKind(StrEnum):
    """Which evidence, beside the contract checks, an answer needs."""

    TEXT = "text"
    CODE = "code"
    CONTRACT_ONLY = "contract_only"


class EnumIndependentJudgeVerdict(StrEnum):
    """What the independent judge said about the answer's content."""

    ACCEPT = "accept"
    REJECT = "reject"
    UNPARSEABLE = "unparseable"
    UNAVAILABLE = "unavailable"


class ModelIndependentJudgeVerdict(BaseModel):
    """One independent judge call: who judged, what it said, what it cost."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    verdict: EnumIndependentJudgeVerdict
    reason: str = Field(default="")
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)


class ModelAnswerJudgeCall(BaseModel):
    """What the independent judge is asked: which backend, and the two messages."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    system_prompt: str = Field(..., min_length=1)
    user_prompt: str = Field(..., min_length=1)


class ModelAnswerGateRequest(BaseModel):
    """The contract verdict plus the judge or test evidence for one answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    task_type: str = Field(..., min_length=1)
    gate_kind: EnumAnswerGateKind
    producer_model_id: str = Field(..., min_length=1)
    contract_gate: ModelQualityGateResult
    judge: ModelIndependentJudgeVerdict | None = None
    named_test_run: ModelNamedTestRun | None = None


__all__ = [
    "EnumAnswerGateKind",
    "EnumIndependentJudgeVerdict",
    "ModelAnswerGateRequest",
    "ModelAnswerJudgeCall",
    "ModelIndependentJudgeVerdict",
]
