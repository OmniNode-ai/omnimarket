# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stored row for one step of one run."""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.enums.enum_run_step_kind import EnumRunStepKind
from omnimarket.enums.enum_run_step_outcome import EnumRunStepOutcome


class ModelRunTraceRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    step_sequence: int = Field(ge=1)
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
    failing_step_sequence: int | None = Field(default=None, ge=1)


__all__ = ["ModelRunTraceRow"]
