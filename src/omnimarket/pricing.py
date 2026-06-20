# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Manifest-backed pricing lookup for cost savings estimation.

Loads the canonical pricing manifest from omnibase_infra once and caches it
for the process lifetime. All cost estimates against the Claude baseline should
use this module rather than hardcoded token prices.
"""

from __future__ import annotations

import functools
import logging
from decimal import Decimal

from omnibase_core.models.delegation.wire import (
    EnumTierCostType,
    ModelPremiumCounterfactual,
    ModelTierCost,
)
from omnibase_infra.models.pricing.model_pricing_entry import ModelPricingEntry
from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

DEFAULT_BASELINE_MODEL = "claude-opus-4-6"
DEFAULT_FRONTIER_COMPARISON_MODELS: tuple[str, ...] = (
    "claude-opus-4-6",
    "claude-sonnet-4-20250514",
    "claude-haiku-3-5",
    "gpt-4o",
)


@functools.cache
def _load_table() -> ModelPricingTable:
    try:
        return ModelPricingTable.from_yaml()
    except Exception as exc:
        logger.warning(
            "Failed to load pricing manifest: %s — falling back to zero table", exc
        )
        return ModelPricingTable.from_dict({"schema_version": "0", "models": {}})


def get_manifest_version_int() -> int:
    """Return the major version of the pricing manifest as an integer.

    The manifest schema_version is a semver string (e.g. "1.0.0"). The DB
    column and event field both store an int, so we return the major component.
    Returns 0 if the version cannot be parsed.
    """
    raw = _load_table().schema_version
    try:
        return int(raw.split(".")[0])
    except (ValueError, IndexError):
        return 0


def estimate_baseline_cost_usd(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    baseline_model: str = DEFAULT_BASELINE_MODEL,
) -> float:
    """Estimate the USD cost of running prompt+completion against the baseline model.

    Uses the canonical pricing manifest. Returns 0.0 if the baseline model is
    not in the manifest (e.g. in test environments with an empty table).
    """
    table = _load_table()
    estimate = table.estimate_cost(baseline_model, prompt_tokens, completion_tokens)
    if estimate.estimated_cost_usd is None:
        logger.debug(
            "Baseline model %r not in pricing manifest; cost_savings_usd will be 0.0",
            baseline_model,
        )
        return 0.0
    return float(estimate.estimated_cost_usd)


def estimate_frontier_costs_usd(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    model_ids: tuple[str, ...] = DEFAULT_FRONTIER_COMPARISON_MODELS,
) -> dict[str, float]:
    """Estimate counterfactual costs for configured frontier comparison models."""
    table = _load_table()
    estimates: dict[str, float] = {}
    for model_id in model_ids:
        estimate = table.estimate_cost(model_id, prompt_tokens, completion_tokens)
        if estimate.estimated_cost_usd is not None:
            estimates[model_id] = float(estimate.estimated_cost_usd)
    return estimates


def build_premium_counterfactual(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    premium_model: str = DEFAULT_BASELINE_MODEL,
    measured: bool = False,
    pricing_source: str = "pricing_manifest",
) -> ModelPremiumCounterfactual | None:
    """Build the pinned premium counterfactual for one delegated task (OMN-13355).

    Resolves the premium model's pinned per-1k input/output price and the
    ``effective_date`` (carried as ``as_of``) from the canonical pricing manifest,
    then computes the counterfactual cost with Decimal precision so the saving
    (counterfactual - actual) is auditable and recomputable from the persisted
    provenance. There is NO live premium API call.

    Returns ``None`` when the premium model is absent from the manifest (e.g. an
    empty test table) — the carrying event then records ``premium_counterfactual``
    as ``None`` rather than a placeholder.
    """
    entry: ModelPricingEntry | None = _load_table().get_entry(premium_model)
    if entry is None:
        logger.debug(
            "Premium model %r not in pricing manifest; no counterfactual emitted",
            premium_model,
        )
        return None

    price_in = Decimal(str(entry.input_cost_per_1k))
    price_out = Decimal(str(entry.output_cost_per_1k))
    counterfactual_cost = (
        price_in * Decimal(prompt_tokens) + price_out * Decimal(completion_tokens)
    ) / Decimal("1000")
    return ModelPremiumCounterfactual(
        model=premium_model,
        price_in_per_1k=price_in,
        price_out_per_1k=price_out,
        as_of=entry.effective_date,
        tokens_in=prompt_tokens,
        tokens_out=completion_tokens,
        counterfactual_cost_usd=counterfactual_cost,
        pricing_source=pricing_source,
        measured=measured,
    )


class ModelTierCostResult(BaseModel):
    """Outcome of computing one delegated task's actual cost under a typed tier
    cost model (OMN-13234).

    - ``cash_cost_usd`` is the actual dollar the tenant pays for this call. It is
      what the projection persists as ``actual_cost_usd`` and subtracts from the
      premium counterfactual to get honest savings.
    - ``headroom_consumed_usd`` is the monthly-budget drawdown for ``budgeted``
      tiers (the accounting cost of in-budget tokens). It is 0 for
      free_local/metered and for the over-cap overage portion.
    - ``measurement_source`` records how the figure was derived, mirrored to the
      projection row for audit (free_local | metered | budgeted_in_budget |
      budgeted_overage | budgeted_split | no_cost_model).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    cash_cost_usd: float = Field(default=0.0, ge=0.0)
    headroom_consumed_usd: float = Field(default=0.0, ge=0.0)
    measurement_source: str = Field(default="")


def compute_tier_cost_usd(
    *,
    cost: ModelTierCost | None,
    prompt_tokens: int,
    completion_tokens: int,
    remaining_budget_usd: float | None = None,
) -> ModelTierCostResult:
    """Compute the actual cost of one delegated task under a typed tier cost model.

    Deterministic and side-effect free (the budget store owns the headroom
    mutation; this function only reports the drawdown). Semantics per
    EnumTierCostType:

    - ``free_local`` → 0 cash, 0 headroom. The local-GPU compute_cost figure is
      computed separately by node_projection_llm_cost from GPU metrics; the
      delegation tier itself bills nothing.
    - ``metered`` → cash = rate_per_1k_usd * total_tokens / 1000, no headroom.
    - ``budgeted`` → tokens covered by ``remaining_budget_usd`` headroom cost 0
      cash (the cap is already paid) and draw down headroom at
      ``rate_per_1k_usd``; tokens past the cap bill cash at
      ``overage_rate_per_1k_usd``. ``remaining_budget_usd`` None means unknown
      headroom → treated as fully exhausted (everything is overage), the
      conservative honest choice.

    ``cost`` None means the tier has not been migrated to the typed model; the
    caller should fall back to the legacy flat ``cost_per_1k_tokens`` path.
    """
    if cost is None:
        return ModelTierCostResult(measurement_source="no_cost_model")

    total_tokens = prompt_tokens + completion_tokens

    if cost.cost_type is EnumTierCostType.FREE_LOCAL:
        return ModelTierCostResult(measurement_source="free_local")

    if cost.cost_type is EnumTierCostType.METERED:
        cash = round(cost.rate_per_1k_usd * total_tokens / 1000.0, 10)
        return ModelTierCostResult(cash_cost_usd=cash, measurement_source="metered")

    # BUDGETED: draw down monthly headroom first, then bill overage.
    in_budget_cost = round(cost.rate_per_1k_usd * total_tokens / 1000.0, 10)
    headroom = float("inf") if remaining_budget_usd is None else remaining_budget_usd
    # remaining_budget_usd None == unknown headroom -> conservative: no headroom.
    if remaining_budget_usd is None:
        headroom = 0.0

    if in_budget_cost <= headroom:
        # Fully inside the cap: 0 cash, draw down headroom by the accounting cost.
        return ModelTierCostResult(
            cash_cost_usd=0.0,
            headroom_consumed_usd=in_budget_cost,
            measurement_source="budgeted_in_budget",
        )

    if headroom <= 0.0:
        # No headroom left: every token is overage.
        cash = round(cost.overage_rate_per_1k_usd * total_tokens / 1000.0, 10)
        return ModelTierCostResult(
            cash_cost_usd=cash,
            measurement_source="budgeted_overage",
        )

    # Split: part inside the cap (headroom-priced 0 cash), part overage. Allocate
    # tokens proportionally to the headroom fraction at the accounting rate.
    if cost.rate_per_1k_usd > 0.0:
        in_budget_tokens = int(headroom * 1000.0 / cost.rate_per_1k_usd)
    else:
        in_budget_tokens = total_tokens
    in_budget_tokens = min(in_budget_tokens, total_tokens)
    overage_tokens = total_tokens - in_budget_tokens
    cash = round(cost.overage_rate_per_1k_usd * overage_tokens / 1000.0, 10)
    consumed = round(cost.rate_per_1k_usd * in_budget_tokens / 1000.0, 10)
    return ModelTierCostResult(
        cash_cost_usd=cash,
        headroom_consumed_usd=consumed,
        measurement_source="budgeted_split",
    )


__all__: list[str] = [
    "DEFAULT_BASELINE_MODEL",
    "DEFAULT_FRONTIER_COMPARISON_MODELS",
    "ModelTierCostResult",
    "build_premium_counterfactual",
    "compute_tier_cost_usd",
    "estimate_baseline_cost_usd",
    "estimate_frontier_costs_usd",
    "get_manifest_version_int",
]
