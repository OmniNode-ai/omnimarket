# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import subprocess
import sys

from omnimarket.models.delegation.llm_cost_routing import ModelLlmModelRegistryLoader
from omnimarket.routing.generated_llm_routing_constants import (
    LLM_ENDPOINT_REFS,
    LOGICAL_MODEL_KEYS,
    EnumLlmEndpointRef,
    EnumLogicalModelKey,
)


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
        [sys.executable, "scripts/generate_llm_routing_constants.py", "--check"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
