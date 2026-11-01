# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model ids are read from configs/model_bindings.v1.yaml, never spelled in code (OMN-21760)."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.validation.validator_contracts_and_di import ValidatorContractsAndDi

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
def test_slice_has_no_literal_model_names() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "omnimarket"
    paths = [
        root / "cli/cli_delegation_cost_demo.py",
        root / "delegation/rubric/lab_measure.py",
        root / "inference/bridge_config_loader.py",
        root / "inference/openrouter_models.py",
        root
        / "nodes/node_delegation_rubric_check_compute/handlers/handler_delegation_route_check.py",
        root / "nodes/node_design_to_plan/handlers/handler_design_to_plan.py",
        root / "nodes/node_merge_sweep_triage_orchestrator/handlers/handler_triage.py",
        root
        / "nodes/node_navigation_history_reducer/handlers/handler_navigation_history_reducer.py",
        root / "nodes/node_plans_today_effect/handlers/handler_plans_today.py",
        root / "nodes/node_shadow_review_effect/handlers/shadow_effects.py",
    ]
    assert all(path.is_file() for path in paths)
    validator = ValidatorContractsAndDi()
    assert any(
        rule.rule_id == "literal_model_name" and rule.enabled
        for rule in validator.contract.rules
    )
    result = validator.validate(paths)
    assert result.metadata is not None
    assert result.metadata.files_processed == len(paths)
    offenders = [
        str(issue)
        for issue in result.issues
        if issue.rule_name in {"literal_model_name", "unparseable_file"}
    ]
    assert not offenders, "\n".join(offenders)


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
