# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_routing_policy_engine.models.model_responder_chain import (
    ModelResponderChainConfig,
)

CONFIG_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_routing_policy_engine"
    / "config"
    / "responder_chains.yaml"
)


def test_responder_chain_yaml_parses() -> None:
    config = ModelResponderChainConfig.from_yaml(CONFIG_PATH)

    chains_by_tier = {chain.tier: chain for chain in config.chains}
    assert set(chains_by_tier) == {"C1", "C2", "C3", "C4"}
    assert all(chain.models for chain in chains_by_tier.values())

    assert [(m.provider, m.model) for m in chains_by_tier["C1"].models] == [
        ("local", "qwen3-14b")
    ]
    assert [(m.provider, m.model) for m in chains_by_tier["C2"].models] == [
        ("local", "qwen3-coder-30b"),
        ("anthropic", "claude-sonnet-4-6"),
    ]
    assert [(m.provider, m.model) for m in chains_by_tier["C3"].models] == [
        ("local", "deepseek-r1-32b"),
        ("anthropic", "claude-sonnet-4-6"),
    ]
    assert [(m.provider, m.model) for m in chains_by_tier["C4"].models] == [
        ("anthropic", "claude-opus-4-6")
    ]
