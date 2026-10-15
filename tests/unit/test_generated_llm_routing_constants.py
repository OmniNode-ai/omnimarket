# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.models.delegation.llm_cost_routing import ModelLlmModelRegistryLoader
from omnimarket.routing.generated_llm_routing_constants import (
    LLM_ENDPOINT_REFS,
    LOGICAL_MODEL_KEYS,
    EnumLlmEndpointRef,
    EnumLogicalModelKey,
)

_RETIRED_MODELS = (
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "deepseek-r1-14b",
    "deepseek-r1-32b",
    "qwen3.6-35b",
    "qwen3-next-80b",
)


@pytest.mark.unit
@pytest.mark.parametrize("model_id", _RETIRED_MODELS)
def test_retired_models_are_not_resolvable(model_id: str) -> None:
    registry = ModelLlmModelRegistryLoader().load()
    with pytest.raises(KeyError, match="not found in registry"):
        registry.get_model(model_id)
    assert model_id not in LOGICAL_MODEL_KEYS


@pytest.mark.unit
def test_retired_endpoint_refs_are_not_generated() -> None:
    assert set(LLM_ENDPOINT_REFS).isdisjoint(
        {
            "ANTHROPIC_DELEGATION_URL",
            "LLM_CODER_FAST_URL",
            "LLM_DEEPSEEK_R1_URL",
            "LLM_QWEN3_NEXT_URL",
        }
    )


@pytest.mark.unit
def test_packaged_tiers_leave_evaluation_model_to_overlay() -> None:
    routing = yaml.safe_load(
        (
            Path(__file__).resolve().parents[2]
            / "src/omnimarket/configs/routing_tiers.yaml"
        ).read_text()
    )
    assert routing["tiers"]
    assert any(tier.get("eval_before_accept") for tier in routing["tiers"])
    assert all(tier.get("eval_model") is None for tier in routing["tiers"])


def test_generated_constants_match_model_registry() -> None:
    registry = ModelLlmModelRegistryLoader().load()

    assert set(LOGICAL_MODEL_KEYS) == set(registry.models)
    assert {item.value for item in EnumLogicalModelKey} == set(registry.models)


def test_generated_endpoint_refs_include_registry_endpoint_envs() -> None:
    registry = ModelLlmModelRegistryLoader().load()
    registry_endpoint_refs = {
        profile.endpoint_env for profile in registry.models.values()
    }

    assert registry_endpoint_refs <= set(LLM_ENDPOINT_REFS)
    assert registry_endpoint_refs <= {item.value for item in EnumLlmEndpointRef}


def test_generated_constants_are_up_to_date() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/generate_llm_routing_constants.py",
            "--topology",
            "/tmp/nonexistent-llm-endpoints.yaml",
            "--check",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
