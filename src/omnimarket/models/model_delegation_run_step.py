# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One numbered step of one delegation run, as the orchestrator emits it.

Shared model: the delegate orchestrator builds it and the run-trace projection
folds it, so it lives in ``omnimarket.models`` rather than in either node's
private models package.

The orchestrator numbers a run's steps from one counter. ``step_sequence`` is
the only ordering authority: two steps may carry one timestamp, and events may
arrive in any order.
"""

from typing import Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from omnimarket.enums.enum_run_step_kind import EnumRunStepKind
from omnimarket.enums.enum_run_step_outcome import EnumRunStepOutcome


class ModelDelegationRunStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    # The key. None of the three has a default: a step that cannot say whose run
    # it belongs to, or where in it, is refused rather than filed somewhere.
    tenant_id: str = Field(min_length=1)
    correlation_id: UUID
    step_sequence: int = Field(ge=1, strict=True)

    step_kind: EnumRunStepKind
    started_at: AwareDatetime
    duration_ms: int | None = Field(default=None, ge=0)
    outcome: EnumRunStepOutcome
    failure_cause: str | None = None

    backend_id: str | None = None
    model_id: str | None = None
    attempt_number: int | None = Field(default=None, ge=1)
    reason: str | None = None
    candidates: tuple[str, ...] = ()

    # Set only on the terminal of a failed run: the earlier step that failed it.
    failing_step_sequence: int | None = Field(default=None, ge=1)

    @field_validator("tenant_id")
    @classmethod
    def tenant_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tenant_id is blank")
        return value

    @model_validator(mode="after")
    def failing_step_belongs_to_a_failed_terminal(self) -> Self:
        if self.failing_step_sequence is None:
            return self
        if (
            self.step_kind is not EnumRunStepKind.TERMINAL
            or self.outcome is not EnumRunStepOutcome.FAILED
        ):
            raise ValueError("only the terminal of a failed run names a failing step")
        if self.failing_step_sequence >= self.step_sequence:
            raise ValueError("the failing step precedes the terminal that names it")
        return self


__all__ = ["ModelDelegationRunStep"]
