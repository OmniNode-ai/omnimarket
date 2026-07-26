# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Regression coverage for routing tier contract model ids."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)
from tests.constants import MODEL_QWEN3_27B_MTP, MODEL_QWEN3_35B_A3B


def test_routing_tiers_declares_sea_reasoning_served_model_context_window() -> None:
    """OMN-12709: SEA's observed local fallback id must resolve without fallback."""
    config_path = Path("src/omnimarket/configs/routing_tiers.yaml")
    config = parse_delegation_config_yaml(config_path.read_text(encoding="utf-8"))

    by_id = {model.id: model for tier in config.tiers for model in tier.models}

    model = by_id[MODEL_QWEN3_27B_MTP]
    assert model.backend_ref == "local-reasoner"
    assert model.max_context_tokens == 24576


def test_routing_tiers_declares_live_local_served_model_ids() -> None:
    """OMN-12721: local routing ids must match the live .201 provider ids."""
    config_path = Path("src/omnimarket/configs/routing_tiers.yaml")
    config = parse_delegation_config_yaml(config_path.read_text(encoding="utf-8"))

    by_id = {model.id: model for tier in config.tiers for model in tier.models}

    assert MODEL_QWEN3_35B_A3B in by_id
    assert MODEL_QWEN3_27B_MTP in by_id
    assert "qwen3-coder-30b" not in by_id
    assert "deepseek-r1-14b" not in by_id


def test_local_tier_declares_local_coder_mlx() -> None:
    """OMN-15180: local-coder-mlx (the .200:8401 MLX endpoint registered as a
    bifrost_delegation.yaml backend by OMN-15155) must be a member of the
    local tier's models list so task_type-based tier selection
    (first_eligible_tier -> backend_id_for_tier -> _select_model_for_task) can
    reach it — before this it was reachable ONLY via an explicit
    resolve_delegation_backend(task_type, backend_id="local-coder-mlx") pin.
    """
    config_path = Path("src/omnimarket/configs/routing_tiers.yaml")
    config = parse_delegation_config_yaml(config_path.read_text(encoding="utf-8"))

    local_tier = next(tier for tier in config.tiers if tier.name == "local")
    by_backend_ref = {model.backend_ref: model for model in local_tier.models}

    assert "local-coder-mlx" in by_backend_ref, (
        "local-coder-mlx must be registered in routing_tiers.yaml's local tier "
        "models[] (OMN-15180)"
    )
    model = by_backend_ref["local-coder-mlx"]
    assert model.id == "mlx-community/Qwen3.6-35B-A3B-8bit"
    assert "code_generation" in model.use_for
