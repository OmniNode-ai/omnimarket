# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the savings estimate (OMN-19979).

The result's money fields are all named ``estimated_*`` and its ``basis`` is the
literal ``estimated``: nothing here reads as a measured savings figure.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.savings_estimate import (
    EnumSavingsBasis,
    ModelClaudeCodePromptRecord,
    ModelEstimateDelegateRoute,
    ModelSavingsEstimateRow,
)
from omnimarket.nodes.node_metering_summary_compute import (
    EnumBaselineState,
    ModelCounterfactualBaseline,
)


class ModelSavingsEstimateRequest(BaseModel):
    """Everything the estimate needs, resolved by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: tuple[ModelClaudeCodePromptRecord, ...] = ()
    baseline_model: str = Field(
        ...,
        min_length=1,
        description="The baseline string Overview's metering rows are keyed by.",
    )
    baseline: ModelCounterfactualBaseline | None = Field(
        default=None,
        description="The pinned price; None when baseline_model is not in the manifest.",
    )
    route: ModelEstimateDelegateRoute

    @model_validator(mode="after")
    def _baseline_matches_the_requested_model(self) -> ModelSavingsEstimateRequest:
        if self.baseline is not None and self.baseline.model != self.baseline_model:
            raise ValueError("resolved baseline must match the requested model")
        return self


class ModelSavingsEstimateResult(BaseModel):
    """Estimated rows and their totals, beside the counts they rest on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis: Literal[EnumSavingsBasis.ESTIMATED] = EnumSavingsBasis.ESTIMATED
    baseline_model: str = Field(..., min_length=1)
    baseline_state: EnumBaselineState
    baseline: ModelCounterfactualBaseline | None = None
    route: ModelEstimateDelegateRoute
    rows: tuple[ModelSavingsEstimateRow, ...] = ()
    prompts_total: int = Field(..., ge=0)
    prompts_estimated: int = Field(..., ge=0)
    prompts_without_usage: int = Field(..., ge=0)
    estimated_baseline_cost_usd: Decimal | None = None
    estimated_delegate_cost_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    estimated_savings_usd: Decimal | None = None

    @model_validator(mode="after")
    def _counts_and_baseline_agree(self) -> ModelSavingsEstimateResult:
        if self.prompts_estimated + self.prompts_without_usage != self.prompts_total:
            raise ValueError("every prompt is either estimated or without usage")
        if self.prompts_estimated != len(self.rows):
            raise ValueError("one row per estimated prompt")
        if (self.baseline is None) != (
            self.baseline_state is EnumBaselineState.UNRESOLVED
        ):
            raise ValueError(
                "baseline_state must be UNRESOLVED exactly when baseline is None"
            )
        if self.estimated_savings_usd is not None and self.baseline is None:
            raise ValueError("an estimated saving must carry its baseline")
        if any(row.baseline_model != self.baseline_model for row in self.rows):
            raise ValueError("every row carries the requested baseline model")
        return self
