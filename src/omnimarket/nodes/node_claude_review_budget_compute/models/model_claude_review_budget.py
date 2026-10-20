# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, result and contract config of the Claude review key budget (OMN-20967)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_claude_review_budget import EnumClaudeReviewBudgetReason
from omnimarket.models.claude_review_budget.model_claude_review_usage import (
    ModelClaudeReviewUsage,
)
from omnimarket.models.claude_review_budget.model_review_budget_admission import (
    ModelReviewBudgetQuote,
)


class ModelClaudeReviewBudgetConfig(BaseModel):
    """The contract's ``budget`` block: the same for every key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str | None = Field(default=None, min_length=1)
    allowance_usd: Decimal = Field(..., gt=0)
    reserve_usd: Decimal = Field(..., ge=0)
    cycle_months: int = Field(..., ge=1)
    cache_write_input_multiplier: Decimal = Field(..., ge=0)
    cache_write_1h_input_multiplier: Decimal = Field(default=Decimal("2"), ge=1)
    batch_multiplier: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)
    cache_read_input_multiplier: Decimal = Field(..., ge=0)

    @model_validator(mode="after")
    def _reserve_below_allowance(self) -> ModelClaudeReviewBudgetConfig:
        if self.reserve_usd >= self.allowance_usd:
            raise ValueError("reserve_usd must be below allowance_usd")
        return self


class ModelClaudeReviewBudgetKey(BaseModel):
    """One key the gateway may spend: its reference, its cycle anchor and the calls it served."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key_ref: str = Field(..., min_length=1)
    cycle_anchor_at: AwareDatetime = Field(
        ..., description="When the key's first cycle started; each cycle runs from it."
    )
    calls: tuple[ModelClaudeReviewUsage, ...] = Field(
        ...,
        description="Calls the spend projection recorded on this key; older cycles are ignored.",
    )

    @model_validator(mode="after")
    def _calls_are_this_keys(self) -> ModelClaudeReviewBudgetKey:
        others = {c.api_key_ref for c in self.calls} - {self.api_key_ref}
        if others:
            raise ValueError(f"calls of another key under {self.api_key_ref!r}")
        return self


class ModelClaudeReviewBudgetRequest(BaseModel):
    """The call about to be made, and the keys in fill-first order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: AwareDatetime
    model_id: str | None = Field(default=None, min_length=1)
    quote: ModelReviewBudgetQuote | None = Field(
        default=None,
        description="Native block-wise maximum charge; overrides legacy prompt_tokens/max_tokens pricing.",
    )
    prompt_tokens: int = Field(
        ..., ge=0, description="The call's counted prompt tokens."
    )
    max_tokens: int = Field(..., ge=1, description="The call's max_tokens.")
    keys: tuple[ModelClaudeReviewBudgetKey, ...] = Field(
        ..., description="Fill-first order: the lowest index that can afford it serves."
    )

    @model_validator(mode="after")
    def _unique_keys(self) -> ModelClaudeReviewBudgetRequest:
        refs = [k.api_key_ref for k in self.keys]
        if len(refs) != len(set(refs)):
            raise ValueError("a key reference appears twice")
        return self


class ModelClaudeReviewKeyBudget(BaseModel):
    """One key's spend this cycle and whether it can take the call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key_ref: str
    cycle_started_at: AwareDatetime
    calls_counted: int
    spent_usd: Decimal
    remaining_usd: Decimal = Field(..., description="Allowance less spend this cycle.")
    affordable: bool = Field(
        ..., description="remaining_usd less the worst case still holds the reserve."
    )


class ModelClaudeReviewBudgetResult(BaseModel):
    """The key chosen, or none and why, with every key's readback."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decided_at: AwareDatetime
    model_id: str | None
    reason: EnumClaudeReviewBudgetReason
    chosen_api_key_ref: str | None
    allowance_usd: Decimal
    reserve_usd: Decimal
    worst_case_usd: Decimal | None = Field(
        ..., description="None only when the model is not priced."
    )
    keys: tuple[ModelClaudeReviewKeyBudget, ...]
    detail: str


__all__ = [
    "ModelClaudeReviewBudgetConfig",
    "ModelClaudeReviewBudgetKey",
    "ModelClaudeReviewBudgetRequest",
    "ModelClaudeReviewBudgetResult",
    "ModelClaudeReviewKeyBudget",
]
