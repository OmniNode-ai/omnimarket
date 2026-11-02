# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the retrospective prompt cost estimate.

A visitor's usage export goes in; every prompt comes out with its task class,
what it cost on the model that answered it, the model the delegate would have
routed it to, and what that would have cost. Every money field is a modelled
estimate priced from the canonical pricing manifest. Each row says how its
token counts were obtained, and the saving is a range: each manifest entry's
confidence and each row's token method widen it by the spread the contract
declares, and ``basis`` names every manifest entry that priced a row.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.savings_estimate import EnumSavingsBasis
from omnimarket.nodes.node_prompt_cost_estimate_compute.models.model_prompt_cost_estimate_config import (
    EnumTokenMethod,
)

PRICING_SOURCE: Literal["omnibase_infra pricing_manifest.yaml"] = (
    "omnibase_infra pricing_manifest.yaml"
)


class EnumExportFormat(StrEnum):
    """The export shapes the estimate reads."""

    CHATGPT_CONVERSATIONS = "chatgpt_conversations"
    CLAUDE_CONVERSATIONS = "claude_conversations"
    CLAUDE_CODE_JSONL = "claude_code_jsonl"
    CSV = "csv"


class EnumModelSource(StrEnum):
    """Where a prompt's answering model came from."""

    EXPORT = "export"
    DECLARED = "declared"


class EnumUnpricedReason(StrEnum):
    """Why a prompt was left out of every total."""

    MODEL_NOT_IN_MANIFEST = "model_not_in_manifest"
    NO_MODEL_NAMED = "no_model_named"


class ModelPromptCostEstimateRequest(BaseModel):
    """One uploaded export, read only after the visitor consented."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consent_given: Literal[True] = Field(
        ...,
        description="The visitor ticked the consent box. Anything else is refused.",
    )
    export_format: EnumExportFormat
    content: str = Field(..., min_length=1, description="The export's text.")
    declared_model: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "The model the visitor says answered prompts the export names no model "
            "for. Applied to those prompts only, and marked declared on each."
        ),
    )


class ModelPromptCostRow(BaseModel):
    """One priced prompt. A prompt that cannot be priced gets no row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis: Literal[EnumSavingsBasis.ESTIMATED] = EnumSavingsBasis.ESTIMATED
    prompt_index: int = Field(..., ge=0)
    conversation_id: str = ""
    occurred_at: AwareDatetime | None = None
    prompt_text: str
    task_type: str = Field(..., min_length=1)
    source_model: str = Field(..., min_length=1)
    model_source: EnumModelSource
    tokens_in: int = Field(..., ge=0)
    tokens_out: int = Field(..., ge=0)
    tokens_estimated: bool = Field(
        ..., description="True when any token count was read from text length."
    )
    token_method: EnumTokenMethod
    actual_cost_usd: Decimal = Field(..., ge=Decimal("0"))
    recommended_model: str = Field(..., min_length=1)
    recommended_tier: str = Field(..., min_length=1)
    delegated_cost_usd: Decimal = Field(..., ge=Decimal("0"))
    saving_usd: Decimal = Field(
        ...,
        ge=Decimal("0"),
        description="The saving at the manifest's listed prices and the counted tokens.",
    )
    saving_usd_low: Decimal = Field(..., ge=Decimal("0"))
    saving_usd_high: Decimal = Field(..., ge=Decimal("0"))

    @model_validator(mode="after")
    def _saving_is_the_difference(self) -> ModelPromptCostRow:
        if self.saving_usd != self.actual_cost_usd - self.delegated_cost_usd:
            raise ValueError("saving_usd must equal actual minus delegated cost")
        if not self.saving_usd_low <= self.saving_usd <= self.saving_usd_high:
            raise ValueError("saving_usd lies within its range")
        if self.tokens_estimated != (self.token_method is not EnumTokenMethod.REPORTED):
            raise ValueError("tokens_estimated is true exactly when not reported")
        return self


class ModelPricingBasis(BaseModel):
    """One pricing manifest entry that priced at least one row, and how far to trust it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(..., min_length=1)
    confidence: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1)
    evidence_authoritative: bool = Field(
        ..., description="The entry's evidence.authoritative; false when absent."
    )
    sample_count: int = Field(..., ge=0)
    checked_against_invoices: bool = Field(
        ...,
        description="Authoritative evidence over at least one reported sample.",
    )
    spread: Decimal = Field(..., ge=Decimal("0"))
    rows_priced: int = Field(..., ge=1)


class ModelUnpricedModel(BaseModel):
    """Prompts left out of every total, grouped by the model they named."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(default="", description="Empty when no model was named.")
    reason: EnumUnpricedReason
    prompts: int = Field(..., ge=1)


class ModelTaskTypeSavings(BaseModel):
    """Totals for one task class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_type: str = Field(..., min_length=1)
    prompts: int = Field(..., ge=1)
    actual_cost_usd: Decimal = Field(..., ge=Decimal("0"))
    delegated_cost_usd: Decimal = Field(..., ge=Decimal("0"))
    saving_usd: Decimal = Field(..., ge=Decimal("0"))
    saving_usd_low: Decimal = Field(..., ge=Decimal("0"))
    saving_usd_high: Decimal = Field(..., ge=Decimal("0"))


class ModelPromptCostEstimateResult(BaseModel):
    """The estimate, its totals and what it left out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis: Literal[EnumSavingsBasis.ESTIMATED] = EnumSavingsBasis.ESTIMATED
    pricing_source: Literal["omnibase_infra pricing_manifest.yaml"] = PRICING_SOURCE
    pricing_manifest_version: str = Field(..., min_length=1)
    export_format: EnumExportFormat
    rows: tuple[ModelPromptCostRow, ...] = ()
    unpriced: tuple[ModelUnpricedModel, ...] = ()
    by_task_type: tuple[ModelTaskTypeSavings, ...] = ()
    prompts_total: int = Field(..., ge=0)
    prompts_priced: int = Field(..., ge=0)
    prompts_unpriced: int = Field(..., ge=0)
    prompts_tokens_estimated: int = Field(
        ..., ge=0, description="Priced rows with any token count read from text."
    )
    tokens_total: int = Field(default=0, ge=0)
    tokens_from_text_length: int = Field(default=0, ge=0)
    tokens_estimated_share: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        le=Decimal("1"),
        description="tokens_from_text_length over tokens_total; zero with no tokens.",
    )
    actual_cost_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    delegated_cost_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    saving_usd: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        description=(
            "Point figure: the saving at the manifest's listed prices and the "
            "counted tokens. The range saving_usd_low to saving_usd_high is the "
            "answer; this figure lies within it."
        ),
    )
    saving_usd_low: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    saving_usd_high: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    pricing_basis: tuple[ModelPricingBasis, ...] = Field(
        default=(),
        description="Every pricing manifest entry that priced at least one row.",
    )

    @model_validator(mode="after")
    def _counts_agree(self) -> ModelPromptCostEstimateResult:
        if self.prompts_priced != len(self.rows):
            raise ValueError("one row per priced prompt")
        if self.prompts_priced + self.prompts_unpriced != self.prompts_total:
            raise ValueError("every prompt is either priced or unpriced")
        if self.prompts_unpriced != sum(group.prompts for group in self.unpriced):
            raise ValueError("every unpriced prompt is listed under its model")
        if self.prompts_tokens_estimated != sum(
            1 for row in self.rows if row.tokens_estimated
        ):
            raise ValueError("prompts_tokens_estimated counts the estimated rows")
        if self.tokens_from_text_length > self.tokens_total:
            raise ValueError("estimated tokens are a part of all tokens")
        if not self.saving_usd_low <= self.saving_usd <= self.saving_usd_high:
            raise ValueError("saving_usd lies within its range")
        return self
