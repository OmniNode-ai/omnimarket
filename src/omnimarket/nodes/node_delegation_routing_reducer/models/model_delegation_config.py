# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 OmniNode Team
"""Compatibility imports and parser for canonical delegation config DTOs."""

from __future__ import annotations

import yaml
from omnibase_core.models.delegation.wire import (
    EnumTierCostType,
    ModelDelegationConfig,
    ModelRoutingTier,
    ModelTierCost,
    ModelTierModel,
)


def _parse_tier_cost(raw_cost: object) -> ModelTierCost | None:
    """Parse the optional typed per-tier `cost` block (OMN-13234).

    None / absent means the tier has not been migrated to the typed cost model;
    the caller falls back to the legacy flat ``cost_per_1k_tokens`` field.
    """
    if raw_cost is None:
        return None
    if not isinstance(raw_cost, dict):
        raise ValueError("routing_tiers.yaml tier 'cost' must be a mapping")
    raw_type = raw_cost.get("cost_type")
    if not isinstance(raw_type, str):
        raise ValueError("routing_tiers.yaml tier 'cost.cost_type' must be a string")
    raw_cap = raw_cost.get("monthly_cap_usd")
    return ModelTierCost(
        cost_type=EnumTierCostType(raw_type),
        rate_per_1k_usd=float(raw_cost.get("rate_per_1k_usd", 0.0)),
        monthly_cap_usd=None if raw_cap is None else float(raw_cap),
        overage_rate_per_1k_usd=float(raw_cost.get("overage_rate_per_1k_usd", 0.0)),
    )


def parse_delegation_config_yaml(yaml_text: str) -> ModelDelegationConfig:
    """Parse delegation config YAML into the canonical DTO.

    Args:
        yaml_text: Contents of routing_tiers.yaml as a string.

    Returns:
        Parsed and validated delegation config.
    """
    raw = yaml.safe_load(yaml_text)
    if not isinstance(raw, dict):
        raise ValueError(
            "routing_tiers.yaml must be a mapping with a top-level 'tiers' key"
        )

    raw_tiers = raw.get("tiers", [])
    if not isinstance(raw_tiers, list):
        raise ValueError("routing_tiers.yaml 'tiers' must be a list")

    tiers = []
    for tier_data in raw_tiers:
        if not isinstance(tier_data, dict):
            raise ValueError("routing_tiers.yaml tier entries must be mappings")

        raw_models = tier_data.get("models", [])
        if not isinstance(raw_models, list):
            raise ValueError("routing_tiers.yaml tier 'models' must be a list")

        models = []
        for m in raw_models:
            if not isinstance(m, dict):
                raise ValueError("routing_tiers.yaml model entries must be mappings")

            raw_use_for = m.get("use_for", [])
            if isinstance(raw_use_for, str):
                use_for = (raw_use_for,)
            elif isinstance(raw_use_for, list):
                use_for = tuple(raw_use_for)
            else:
                raise ValueError(
                    "routing_tiers.yaml model 'use_for' must be a string or list"
                )

            models.append(
                ModelTierModel(
                    id=m["id"],
                    backend_ref=m["backend_id"],
                    max_context_tokens=m["max_context_tokens"],
                    use_for=use_for,
                    fast_path_threshold_tokens=m.get("fast_path_threshold_tokens"),
                )
            )
        tiers.append(
            ModelRoutingTier(
                name=tier_data["name"],
                models=tuple(models),
                eval_before_accept=tier_data.get("eval_before_accept", False),
                eval_model=tier_data.get("eval_model"),
                max_retries=tier_data.get("max_retries", 0),
                cost_per_1k_tokens=float(tier_data.get("cost_per_1k_tokens", 0.0)),
                cost=_parse_tier_cost(tier_data.get("cost")),
            )
        )
    return ModelDelegationConfig(tiers=tuple(tiers))


__all__: list[str] = ["ModelDelegationConfig", "parse_delegation_config_yaml"]
