# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model ids are read from configs/model_bindings.v1.yaml, never spelled in code (OMN-21760)."""

from __future__ import annotations

import pytest

from omnimarket.handlers.model_binding_reader import (
    model_binding,
    model_binding_section,
)
from omnimarket.inference.bridge_config_loader import LOCAL_ENDPOINT_MODEL_KEYS
from omnimarket.inference.openrouter_models import (
    EnumOpenRouterTier,
    get_openrouter_models,
)
from omnimarket.nodes.node_design_to_plan.handlers.handler_design_to_plan import (
    HandlerDesignToPlan,
)


@pytest.mark.unit
def test_openrouter_catalog_comes_from_the_bindings_file() -> None:
    rows = model_binding_section("openrouter_free_models")
    models = get_openrouter_models()
    assert [m.model_id for m in models] == [r["model_id"] for r in rows]
    assert len(get_openrouter_models(EnumOpenRouterTier.SMALL_FREE)) == 6
    assert len(get_openrouter_models(EnumOpenRouterTier.LARGE_FREE)) == 6
    assert all(m.model_id.endswith(":free") for m in models)


@pytest.mark.unit
def test_bridge_model_keys_come_from_the_bindings_file() -> None:
    declared = {row["key"] for row in model_binding_section("bridge_model_keys")}
    assert declared
    assert declared == LOCAL_ENDPOINT_MODEL_KEYS


@pytest.mark.unit
def test_binding_fails_closed_on_a_missing_name() -> None:
    with pytest.raises(KeyError):
        model_binding("no_such_binding")
    with pytest.raises(KeyError):
        model_binding_section("no_such_section")


@pytest.mark.unit
def test_section_is_a_copy_so_a_caller_cannot_change_the_declared_policy() -> None:
    policy = model_binding_section("triage_routing_policy")
    policy["primary"] = "changed"
    assert model_binding_section("triage_routing_policy")["primary"] != "changed"


@pytest.mark.unit
def test_design_to_plan_model_key_is_injected_or_read_from_the_bindings() -> None:
    assert HandlerDesignToPlan()._plan_model_key == model_binding(
        "design_to_plan_model_key"
    )
    assert HandlerDesignToPlan(plan_model_key="injected")._plan_model_key == "injected"
