# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-9272: contract.yaml model_routing endpoint_env must align with bridge_config_loader.

The bridge_config_loader maps short model keys (qwen3-coder, deepseek-r1) to
env vars (LLM_CODER_URL, LLM_DEEPSEEK_R1_URL). The pr_review_bot contract.yaml
had LLM_QWEN3_CODER_URL for the reviewer model_routing entry which is not
registered in the bridge loader, causing every run to get empty model_configs.

This test locks down the alignment: the env vars declared in contract.yaml
model_routing must be present in the bridge loader registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.inference.bridge_config_loader import _MODEL_KEY_REGISTRY

_CONTRACT_PATH = (
    Path(__file__).parents[3] / "src/omnimarket/nodes/node_pr_review_bot/contract.yaml"
)

_BRIDGE_LOADER_ENV_VARS: frozenset[str] = frozenset(
    url_env for _, url_env, _ in _MODEL_KEY_REGISTRY
)


@pytest.mark.unit
def test_contract_model_routing_endpoint_envs_match_bridge_loader() -> None:
    """All model_routing endpoint_env values in contract.yaml must be registered
    in the bridge_config_loader._MODEL_KEY_REGISTRY so bridge config is populated.

    Regression guard for OMN-9272: contract had LLM_QWEN3_CODER_URL which is
    not in the registry, causing empty model_configs and Unknown model_key errors.
    """
    data = yaml.safe_load(_CONTRACT_PATH.read_text())
    model_routing: dict[str, object] = data.get("model_routing", {})

    unregistered: list[str] = []
    for role, declaration in model_routing.items():
        if not isinstance(declaration, dict):
            continue
        endpoint_env: str = declaration.get("endpoint_env", "")
        if not endpoint_env:
            continue
        if endpoint_env not in _BRIDGE_LOADER_ENV_VARS:
            unregistered.append(f"{role} -> {endpoint_env}")

    assert not unregistered, (
        "contract.yaml model_routing endpoint_env vars not in bridge_config_loader registry: "
        f"{unregistered!r}. "
        "Either update contract.yaml to use a registered env var, or add the key to "
        "_MODEL_KEY_REGISTRY in bridge_config_loader.py."
    )
