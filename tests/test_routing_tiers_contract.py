# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Regression coverage for routing tier contract model ids."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)


def test_routing_tiers_declares_sea_reasoning_served_model_context_window() -> None:
    """OMN-12709: SEA's observed local fallback id must resolve without fallback."""
    config_path = Path("src/omnimarket/configs/routing_tiers.yaml")
    config = parse_delegation_config_yaml(config_path.read_text(encoding="utf-8"))

    by_id = {model.id: model for tier in config.tiers for model in tier.models}

    model = by_id["Qwen3.6-27B-MTP-IQ4_XS.gguf"]
    assert model.backend_ref == "local-reasoner"
    assert model.max_context_tokens == 24576
