# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the GLM usage record (OMN-20287)."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.enums.enum_glm_allowance import EnumGlmRunOutcome
from omnimarket.models.glm_allowance.model_glm_allowance_policy import (
    ModelGlmAllowancePolicy,
)
from omnimarket.models.glm_allowance.model_glm_events import (
    ModelGlmRefusalObserved,
    ModelGlmUsageRecorded,
)


class ModelGlmRunFacts(BaseModel):
    """What a harness delegate receipt and its transcript say about one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., min_length=1)
    started_at: AwareDatetime
    duration_s: float = Field(..., ge=0)
    model: str = Field(..., min_length=1)
    task_class: str | None
    lane: str | None
    outcome: EnumGlmRunOutcome
    error_class: str | None
    input_tokens: int = Field(..., ge=0)
    cached_input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    retry_statuses: tuple[int | None, ...] = Field(
        ..., description="error_status of each api_retry note in the run's transcript."
    )
    provider_codes: tuple[str, ...] = Field(
        ..., description="Provider error codes the run's transcript carried."
    )


class ModelGlmUsageRecordRequest(BaseModel):
    """One run to record, under the deployment's policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: ModelGlmAllowancePolicy
    run: ModelGlmRunFacts


class ModelGlmUsageRecordResult(BaseModel):
    """The usage event, and the refusal event when the run met a cap refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    usage: ModelGlmUsageRecorded
    refusal: ModelGlmRefusalObserved | None


__all__ = [
    "ModelGlmRunFacts",
    "ModelGlmUsageRecordRequest",
    "ModelGlmUsageRecordResult",
]
