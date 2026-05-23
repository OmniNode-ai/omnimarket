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


__all__: list[str] = [
    "DEFAULT_BASELINE_MODEL",
    "DEFAULT_FRONTIER_COMPARISON_MODELS",
    "estimate_baseline_cost_usd",
    "estimate_frontier_costs_usd",
    "get_manifest_version_int",
]
