# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared typed commands and state for the review budget authority."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.claude_review_budget.model_claude_review_usage import (
    ModelClaudeReviewUsage,
)


class ModelReviewBudgetBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ModelReviewInputBlock(ModelReviewBudgetBase):
    tokens: int = Field(ge=0)
    cache_ttl: Literal["uncached", "5m", "1h"] = "uncached"


class ModelReviewBudgetQuote(ModelReviewBudgetBase):
    model_id: str = Field(min_length=1)
    blocks: tuple[ModelReviewInputBlock, ...]
    schema_tokens: int = Field(default=0, ge=0)
    max_tokens: int = Field(ge=1)
    mode: Literal["synchronous", "batch"] = "synchronous"


class ModelReviewKeyLimit(ModelReviewBudgetBase):
    cycle_anchor_at: AwareDatetime | None = None
    allowance_usd: Decimal = Field(gt=0)
    reserve_usd: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def reserve_fits(self) -> "ModelReviewKeyLimit":
        if self.reserve_usd >= self.allowance_usd:
            raise ValueError("reserve must be below allowance")
        return self


class ModelReviewPersonaPolicy(ModelReviewBudgetBase):
    """A deployment-named tier and its explicit admission behavior, from the overlay."""

    tier: str = Field(min_length=1)
    enforce_pacing: bool


class ModelReviewBudgetPolicy(ModelReviewBudgetBase):
    """Trusted versioned overlay, injected at the authority, never in a caller command."""

    version: str = Field(min_length=1)
    cycle_anchor_at: AwareDatetime
    aggregate_limit_usd: Decimal = Field(gt=0)
    pacing_reserve_usd: Decimal = Field(ge=0)
    key_limits: dict[str, ModelReviewKeyLimit]
    personas: dict[str, ModelReviewPersonaPolicy]


class ModelReviewBudgetCommand(ModelReviewBudgetBase):
    operation: Literal["reserve", "started", "settle", "release", "reconcile"]
    call_id: UUID
    api_key_ref: str | None = Field(default=None, min_length=1)
    model_id: str | None = Field(default=None, min_length=1)
    persona_id: str | None = Field(default=None, min_length=1)
    policy_version: str | None = Field(default=None, min_length=1)
    quote: ModelReviewBudgetQuote | None = None
    mode: Literal["synchronous", "batch"] = "synchronous"
    usage: ModelClaudeReviewUsage | None = None
    zero_charge_evidence: str | None = Field(default=None, min_length=1)
    reconciled_spend_usd: Decimal | None = Field(default=None, ge=0)
    aggregate_reconciled_spend_usd: Decimal | None = Field(default=None, ge=0)
    reconcile_evidence: str | None = Field(default=None, min_length=1)
    resolved_call_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def matching_usage(self) -> "ModelReviewBudgetCommand":
        if self.operation == "reserve" and any(
            value is None
            for value in (
                self.api_key_ref,
                self.model_id,
                self.persona_id,
                self.policy_version,
                self.quote,
            )
        ):
            raise ValueError(
                "reserve requires a key, model, persona, policy version and quote"
            )
        if self.quote is not None and self.quote.model_id != self.model_id:
            raise ValueError("quote and command model differ")
        if self.operation == "settle":
            if self.usage is None:
                raise ValueError("settle requires measured usage")
            if (
                self.usage.call_id != self.call_id
                or (
                    self.api_key_ref is not None
                    and self.usage.api_key_ref != self.api_key_ref
                )
                or (self.model_id is not None and self.usage.model_id != self.model_id)
            ):
                raise ValueError("usage does not identify this reservation")
        if self.operation == "reconcile" and (
            self.reconciled_spend_usd is None
            or not self.reconcile_evidence
            or not self.api_key_ref
        ):
            raise ValueError("reconcile requires a provider balance and evidence")
        return self


class ModelReviewBudgetReservation(ModelReviewBudgetBase):
    fingerprint: str
    api_key_ref: str
    model_id: str
    mode: Literal["synchronous", "batch"]
    maximum_usd: Decimal = Field(ge=0)
    status: Literal["reserved", "started", "settled", "released", "reconciled"]
    usage: ModelClaudeReviewUsage | None = None


class ModelReviewBudgetState(ModelReviewBudgetBase):
    cycle_started_at: AwareDatetime | None = None
    last_as_of: AwareDatetime | None = None
    aggregate_settled_usd: Decimal = Field(default=Decimal(0), ge=0)
    key_cycle_started_at: dict[str, AwareDatetime] = Field(default_factory=dict)
    settled_usd: dict[str, Decimal] = Field(default_factory=dict)
    reservations: dict[str, ModelReviewBudgetReservation] = Field(default_factory=dict)
    reconciliations: dict[str, str] = Field(default_factory=dict)


class ModelReviewBudgetTransition(ModelReviewBudgetBase):
    state: ModelReviewBudgetState
    command: ModelReviewBudgetCommand
    policy: ModelReviewBudgetPolicy
    as_of: AwareDatetime
    worst_case_usd: Decimal | None = Field(default=None, ge=0)
    measured_usd: Decimal | None = Field(default=None, ge=0)


class ModelReviewBudgetDecision(ModelReviewBudgetBase):
    admitted: bool
    reason: str
    call_id: UUID
    key_committed_usd: Decimal
    aggregate_committed_usd: Decimal
    reserved_usd: Decimal
    policy_version: str


class ModelReviewBudgetReduction(ModelReviewBudgetBase):
    state: ModelReviewBudgetState
    decision: ModelReviewBudgetDecision
