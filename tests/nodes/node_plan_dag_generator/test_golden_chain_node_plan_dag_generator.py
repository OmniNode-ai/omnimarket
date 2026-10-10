# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registered package -> contract -> typed handler -> typed result."""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from importlib.resources import files
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_plan_dag_generator import (
    EnumWorkUnitType,
    HandlerPlanDagDefault,
    ModelPlanDag,
    ModelPlanDagRequest,
)

pytestmark = pytest.mark.unit


def test_registered_contract_executes_typed_plan_dag_generator() -> None:
    entries = [
        entry
        for entry in entry_points(group="onex.nodes")
        if entry.name == "node_plan_dag_generator"
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.value == "omnimarket.nodes.node_plan_dag_generator"
    package = entry.load()
    contract = yaml.safe_load(
        Path(str(files(package).joinpath("contract.yaml"))).read_text()
    )
    assert contract["name"] == entry.name
    assert contract["node_type"] == "compute"
    assert contract["capabilities"][0]["name"] == "nl.plan.dag.compute"
    assert contract["io_operations"][0]["operation"] == "generate_plan_dag"
    assert contract["io_operations"][0]["protocol_method"] == "handle"

    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    input_module, input_name = binding["input_model"].rsplit(".", 1)
    output_module, output_name = binding["output_model"].rsplit(".", 1)
    request_type = getattr(importlib.import_module(input_module), input_name)
    result_type = getattr(importlib.import_module(output_module), output_name)
    assert request_type is ModelPlanDagRequest
    assert result_type is ModelPlanDag
    assert request_type is package.ModelPlanDagRequest
    assert result_type is package.ModelPlanDag
    assert handler_type is package.HandlerPlanDagDefault
    assert handler_type is HandlerPlanDagDefault
    for field, model_type in (
        ("input_model", request_type),
        ("output_model", result_type),
    ):
        model_binding = contract[field]
        assert (
            getattr(
                importlib.import_module(model_binding["module"]), model_binding["name"]
            )
            is model_type
        )

    correlation_id = UUID("12345678-1234-5678-1234-567812345678")
    request = ModelPlanDagRequest(
        intent_id="intent-1",
        intent_type="BUG_FIX",
        intent_summary="Fix the login failure",
        correlation_id=correlation_id,
    )
    handler = handler_type()
    result = handler.handle(request)
    assert isinstance(result, ModelPlanDag)
    assert result.dag_id
    assert result.intent_id == request.intent_id
    assert len(result.nodes) == 3
    assert len(result.edges) == 2
    assert result.orphaned_unit_ids == frozenset()
    ordered = result.topological_sort()
    assert [unit.unit_type for unit in ordered] == [
        EnumWorkUnitType.INVESTIGATION,
        EnumWorkUnitType.BUG_FIX,
        EnumWorkUnitType.TEST_SUITE,
    ]
    assert all(request.intent_summary in unit.description for unit in ordered)
    assert handler.handler_type == "node_handler"
    assert handler.handler_category == "compute"
    assert handler.handler_key == "default"
