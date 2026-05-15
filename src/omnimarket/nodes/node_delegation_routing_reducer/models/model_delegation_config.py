# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 OmniNode Team
"""Compatibility imports and parser for canonical delegation config DTOs."""

from __future__ import annotations

import yaml
from omnibase_compat.contracts.delegation.wire import (
    ModelDelegationConfig,
    ModelRoutingTier,
    ModelTierModel,
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

            use_for = m.get("use_for", [])
            if not isinstance(use_for, list):
                raise ValueError("routing_tiers.yaml model 'use_for' must be a list")

            models.append(
                ModelTierModel(
                    id=m["id"],
                    backend_ref=m["backend_id"],
                    max_context_tokens=m["max_context_tokens"],
                    use_for=tuple(use_for),
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
            )
        )
    return ModelDelegationConfig(tiers=tuple(tiers))


__all__: list[str] = ["ModelDelegationConfig", "parse_delegation_config_yaml"]
