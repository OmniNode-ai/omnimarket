# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Estimate historical prompt savings through the shared pricing helpers."""

from __future__ import annotations

from decimal import Decimal

from omnimarket.models.savings_estimate import ModelSavingsEstimateRow
from omnimarket.nodes.node_metering_summary_compute import (
    EnumBaselineState,
    counterfactual_cost_usd,
)
from omnimarket.nodes.node_savings_estimate_compute.models.model_savings_estimate_request import (
    ModelSavingsEstimateRequest,
    ModelSavingsEstimateResult,
)
from omnimarket.pricing import compute_tier_cost_usd


class HandlerSavingsEstimate:
    """Compute deterministic estimated rows and totals from recorded usage."""

    def handle(
        self, payload: ModelSavingsEstimateRequest
    ) -> ModelSavingsEstimateResult:
        baseline = payload.baseline
        baseline_state = (
            EnumBaselineState.RESOLVED
            if baseline is not None
            else EnumBaselineState.UNRESOLVED
        )
        rows: list[ModelSavingsEstimateRow] = []
        prompts_without_usage = 0
        baseline_total = Decimal("0")
        delegate_total = Decimal("0")
        savings_total = Decimal("0")
        for record in payload.records:
            if not record.has_usage:
                prompts_without_usage += 1
                continue
            baseline_cost = (
                counterfactual_cost_usd(
                    baseline=baseline,
                    tokens_in=record.tokens_in,
                    tokens_out=record.tokens_out,
                )
                if baseline is not None
                else None
            )
            delegate_cost = Decimal(
                str(
                    compute_tier_cost_usd(
                        cost=payload.route.cost,
                        prompt_tokens=record.tokens_in,
                        completion_tokens=record.tokens_out,
                    ).cash_cost_usd
                )
            )
            savings = (
                baseline_cost - delegate_cost if baseline_cost is not None else None
            )
            rows.append(
                ModelSavingsEstimateRow(
                    session_id=record.session_id,
                    prompt_id=record.prompt_id,
                    project=record.project,
                    occurred_at=record.occurred_at,
                    source_model=record.model,
                    tokens_in=record.tokens_in,
                    tokens_out=record.tokens_out,
                    baseline_model=payload.baseline_model,
                    baseline_state=baseline_state,
                    pricing_manifest_version=(
                        baseline.pricing_manifest_version
                        if baseline is not None
                        else None
                    ),
                    estimated_baseline_cost_usd=baseline_cost,
                    delegate_tier=payload.route.tier_name,
                    delegate_model=payload.route.model,
                    estimated_delegate_cost_usd=delegate_cost,
                    estimated_savings_usd=savings,
                )
            )
            delegate_total += delegate_cost
            if baseline_cost is not None:
                baseline_total += baseline_cost
            if savings is not None:
                savings_total += savings

        has_baselined_rows = baseline is not None and bool(rows)
        return ModelSavingsEstimateResult(
            baseline_model=payload.baseline_model,
            baseline_state=baseline_state,
            baseline=baseline,
            route=payload.route,
            rows=tuple(rows),
            prompts_total=len(payload.records),
            prompts_estimated=len(rows),
            prompts_without_usage=prompts_without_usage,
            estimated_baseline_cost_usd=baseline_total if has_baselined_rows else None,
            estimated_delegate_cost_usd=delegate_total,
            estimated_savings_usd=savings_total if has_baselined_rows else None,
        )
