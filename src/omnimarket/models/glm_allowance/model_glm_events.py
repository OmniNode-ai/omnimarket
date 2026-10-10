# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Events the GLM allowance nodes emit for the liveness projection (OMN-20287).

Counts, credits, models and outcomes only: no prompt, no answer and no credential
reference, so the events are safe for the projection to hold.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.enums.enum_glm_allowance import EnumGlmRefusalScope, EnumGlmRunOutcome


class ModelGlmUsageRecorded(BaseModel):
    """One harness run on the plan and the credits it spent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    provider_id: str
    model: str
    task_class: str | None
    lane: str | None
    started_at: AwareDatetime
    outcome: EnumGlmRunOutcome
    input_tokens: int = Field(..., ge=0)
    cached_input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    credits: float = Field(..., ge=0)
    peak_factor: float = Field(..., gt=0)
    rated: bool = Field(
        ..., description="False when the policy has no rate for the model."
    )


class ModelGlmRefusalObserved(BaseModel):
    """The provider refused a run's call for capacity, and what became of the run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    provider_id: str
    model: str
    task_class: str | None
    lane: str | None
    observed_at: AwareDatetime
    scope: EnumGlmRefusalScope
    http_status: int | None
    provider_code: str | None
    count: int = Field(..., ge=1, description="Refused attempts seen in the run.")
    run_outcome: EnumGlmRunOutcome = Field(
        ...,
        description="done: the run retried through it; otherwise the run ended on it or beside it.",
    )


__all__ = ["ModelGlmRefusalObserved", "ModelGlmUsageRecorded"]
