# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared input and judge evidence for delegated-answer acceptance (OMN-20489)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    model_validator,
)

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


class EnumNamedTestVerdict(StrEnum):
    """What a code answer's named tests said (OMN-17427).

    ``untested`` is the verdict for a request that named no tests: the answer
    is decided by its contract checks and is never recorded as tested.
    ``not_run`` is a request that named tests no runner ran, which is refused.
    """

    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    UNTESTED = "untested"


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
    # OMN-17427: whether the request named tests. With none named and no run,
    # a code answer is UNTESTED rather than refused as INSUFFICIENT.
    # Consumer first: v0.4.314 forbids the named_tests_requested wire key, so
    # the value is decoded and retained for in-process callers, never emitted
    # until a consumer carrying this decoder is released.
    _named_tests_requested: bool = PrivateAttr(default=False)

    @property
    def named_tests_requested(self) -> bool:
        """Whether the request named tests; not a wire field yet."""
        return self._named_tests_requested

    @model_validator(mode="wrap")
    @classmethod
    def _retain_named_tests_requested_without_emitting_it(
        cls, data: Any, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        if not isinstance(data, Mapping) or "named_tests_requested" not in data:
            return handler(data)
        requested = data["named_tests_requested"]
        if not isinstance(requested, bool):
            raise ValueError("named_tests_requested must be a bool")
        return handler(
            {k: v for k, v in data.items() if k != "named_tests_requested"}
        ).retain_named_tests_requested(requested)

    def retain_named_tests_requested(self, requested: bool) -> Self:
        """Attach the in-process flag without changing the frozen wire fields."""
        self._named_tests_requested = requested
        return self


__all__ = [
    "EnumAnswerGateKind",
    "EnumIndependentJudgeVerdict",
    "EnumNamedTestVerdict",
    "ModelAnswerGateRequest",
    "ModelAnswerJudgeCall",
    "ModelIndependentJudgeVerdict",
]
