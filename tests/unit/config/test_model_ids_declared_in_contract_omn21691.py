# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21691 AC2: a model id is read from the declaring contract or config, never from code.

Each moved value is declared in the owning node contract or a ``configs/*.v1.yaml`` file and
reaches the code through a loader that fails closed on a gap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.inference import bridge_config_loader
from omnimarket.inference.openrouter_models import (
    EnumOpenRouterTier,
    _load_catalog,
    get_openrouter_models,
)
from omnimarket.nodes.node_design_to_plan.handlers.handler_design_to_plan import (
    HandlerDesignToPlan,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.handlers.handler_triage import (
    HandlerTriageOrchestrator,
)
from omnimarket.nodes.node_navigation_history_reducer.handlers import (
    handler_navigation_history_reducer,
)
from omnimarket.nodes.node_thread_reply_effect.handlers import handler_thread_reply


@pytest.mark.unit
def test_openrouter_catalog_is_read_from_the_declared_file() -> None:
    models = get_openrouter_models()
    assert len(models) == 12
    assert {m.tier for m in models} == set(EnumOpenRouterTier)
    assert all(m.model_id.endswith(":free") for m in models)
    assert get_openrouter_models(EnumOpenRouterTier.LARGE_FREE) == tuple(
        m for m in models if m.tier == EnumOpenRouterTier.LARGE_FREE
    )


@pytest.mark.unit
def test_openrouter_catalog_fails_closed_on_a_malformed_declaration(
    tmp_path: Path,
) -> None:
    declared = tmp_path / "catalog.yaml"
    declared.write_text("models: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'models' must be a list"):
        _load_catalog(declared)
    with pytest.raises(FileNotFoundError):
        _load_catalog(tmp_path / "absent.yaml")


@pytest.mark.unit
def test_bridge_model_key_registry_is_read_from_the_declared_file() -> None:
    registry = bridge_config_loader._MODEL_KEY_REGISTRY
    assert [key for key, _url, _model in registry] == [
        "qwen3-coder",
        "qwen3-14b",
        "deepseek-r1",
        "qwen3-next",
    ]
    assert {
        key for key, _url, _model in registry
    } == bridge_config_loader.LOCAL_ENDPOINT_MODEL_KEYS


@pytest.mark.unit
def test_triage_routing_policy_comes_from_its_contract_unless_injected() -> None:
    declared = HandlerTriageOrchestrator()._routing_policy
    assert declared["primary"] == declared["ci_override"]["primary"]
    assert declared["primary"] != declared["fallback"]
    injected = {"primary": "a", "fallback": "b"}
    assert (
        HandlerTriageOrchestrator(routing_policy=injected)._routing_policy is injected
    )


@pytest.mark.unit
def test_design_to_plan_model_key_comes_from_its_contract_unless_injected() -> None:
    assert HandlerDesignToPlan()._plan_model_key
    assert HandlerDesignToPlan(plan_model_key="injected")._plan_model_key == "injected"


@pytest.mark.unit
def test_embedding_model_comes_from_the_navigation_contract() -> None:
    assert handler_navigation_history_reducer._DEFAULT_EMBEDDING_MODEL


@pytest.mark.unit
def test_thread_reply_registries_come_from_the_node_contract() -> None:
    assert set(handler_thread_reply._BASE_REGISTRY) == {"deepseek-r1-14b"}
    assert handler_thread_reply._BASE_REGISTRY["deepseek-r1-14b"]["health_path"]
    assert set(handler_thread_reply._CONTRACT_BACKENDS.values()) == {
        "local-coder",
        "cloud-glm",
    }
