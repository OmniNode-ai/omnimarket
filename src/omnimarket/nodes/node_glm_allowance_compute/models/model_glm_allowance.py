# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the GLM allowance check (OMN-20287)."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.enums.enum_glm_allowance import (
    EnumGlmAllowanceVerdict,
    EnumGlmSkipReason,
)
from omnimarket.models.glm_allowance.model_glm_allowance_policy import (
    ModelGlmAllowancePolicy,
)
from omnimarket.models.glm_allowance.model_glm_usage_record import (
    ModelGlmRefusalRecord,
    ModelGlmUsageRecord,
)


class ModelGlmChainRung(BaseModel):
    """One rung of a resolved escalation chain, as the overlay orders it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    is_glm: bool
    cost_rank: int = Field(
        ..., ge=0, description="Lower is cheaper; the overlay declares it."
    )


class ModelGlmAllowanceRequest(BaseModel):
    """The usage of the plan, the chain to order, and the call that wants a rung."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    policy: ModelGlmAllowancePolicy
    usage: tuple[ModelGlmUsageRecord, ...]
    refusals: tuple[ModelGlmRefusalRecord, ...]
    task_class: str = Field(..., min_length=1)
    budget_s: int | None = Field(
        ..., ge=1, description="The call's harness budget; None when unknown."
    )
    estimated_credits: float | None = Field(..., ge=0)
    chain: tuple[ModelGlmChainRung, ...] = Field(..., min_length=1)


class ModelGlmWindowState(BaseModel):
    """What is left of one counting period."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cap: float
    used: float
    remaining: float
    reserve: float
    usable: float = Field(
        ..., description="remaining less the reserve, never below zero."
    )
    remaining_fraction: float
    resets_at: AwareDatetime | None = Field(
        ..., description="Window: when its oldest spend expires. Week: the next reset."
    )


class ModelGlmSkippedRung(BaseModel):
    """A GLM rung dropped from the chain, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung: str
    reason: EnumGlmSkipReason


class ModelGlmAllowanceResult(BaseModel):
    """The allowance left, and the chain the GLM rung consult leaves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated_at: AwareDatetime
    provider_id: str
    task_class: str
    verdict: EnumGlmAllowanceVerdict
    window: ModelGlmWindowState
    week: ModelGlmWindowState
    estimated_credits: float
    blocked_until: AwareDatetime | None
    ordered_rungs: tuple[str, ...]
    skipped: tuple[ModelGlmSkippedRung, ...]
    unrated_models: tuple[str, ...]


__all__ = [
    "ModelGlmAllowanceRequest",
    "ModelGlmAllowanceResult",
    "ModelGlmChainRung",
    "ModelGlmSkippedRung",
    "ModelGlmWindowState",
]
