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

from omnibase_core.models.delegation.wire import ModelPremiumCounterfactual
from omnibase_infra.models.pricing.model_pricing_entry import ModelPricingEntry
from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

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


__all__: list[str] = [
    "DEFAULT_BASELINE_MODEL",
    "DEFAULT_FRONTIER_COMPARISON_MODELS",
    "build_premium_counterfactual",
    "estimate_baseline_cost_usd",
    "estimate_frontier_costs_usd",
    "get_manifest_version_int",
]
