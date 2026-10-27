# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The committed routing ladder as the routing authority loads it (OMN-20376).

``routing_tiers.yaml`` names no vendor backend in the free tier: the one rung of
``cheap_frontier`` is declared by a ``placement`` in ``bifrost_delegation.yaml``
and appended when the ladder is loaded. A test that asks what a tier serves
reads the placed ladder, not the raw tiers file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_backend_placements,
)
from omnimarket.models.delegation.wire import (
    ModelDelegationConfig,
    parse_delegation_config_yaml,
)
from omnimarket.routing.backend_placement import apply_backend_placements

_CONFIGS = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "configs"


def placed_routing_config() -> ModelDelegationConfig:
    """The committed tiers file with the committed bifrost placements applied."""
    config = parse_delegation_config_yaml(
        (_CONFIGS / "routing_tiers.yaml").read_text(encoding="utf-8")
    )
    return apply_backend_placements(
        config,
        load_bifrost_backend_placements(
            config_path=_CONFIGS / "bifrost_delegation.yaml"
        ),
    )


def placed_tiers_raw() -> dict[str, dict[str, Any]]:
    """The raw tiers mapping by name, each ``models`` list read from the placed ladder."""
    loaded = yaml.safe_load(
        (_CONFIGS / "routing_tiers.yaml").read_text(encoding="utf-8")
    )
    placed = {tier.name: tier for tier in placed_routing_config().tiers}
    tiers: dict[str, dict[str, Any]] = {}
    for raw in loaded["tiers"]:
        models = [
            {
                "id": model.id,
                "backend_id": model.backend_ref,
                "max_context_tokens": model.max_context_tokens,
                "use_for": list(model.use_for),
                "fast_path_threshold_tokens": model.fast_path_threshold_tokens,
            }
            for model in placed[raw["name"]].models
        ]
        tiers[raw["name"]] = {**raw, "models": models}
    return tiers
