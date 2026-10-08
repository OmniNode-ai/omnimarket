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

from omnimarket.nodes.node_ambiguity_gate import (
    AmbiguityGateError,
    EnumAmbiguityType,
    EnumGateVerdict,
    HandlerAmbiguityGateDefault,
    ModelGateCheckRequest,
    ModelGateCheckResult,
)

pytestmark = pytest.mark.unit


def test_registered_contract_executes_typed_ambiguity_gate() -> None:
    entries = [
        entry
        for entry in entry_points(group="onex.nodes")
        if entry.name == "node_ambiguity_gate"
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.value == "omnimarket.nodes.node_ambiguity_gate"
    package = entry.load()
    contract = yaml.safe_load(
        Path(str(files(package).joinpath("contract.yaml"))).read_text()
    )
    assert contract["name"] == entry.name
    assert contract["node_type"] == "compute"
    assert contract["capabilities"][0]["name"] == "nl.ambiguity.gate.compute"
    assert contract["io_operations"][0]["operation"] == "check"
    assert contract["io_operations"][0]["protocol_method"] == "handle"

    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    input_module, input_name = binding["input_model"].rsplit(".", 1)
    output_module, output_name = binding["output_model"].rsplit(".", 1)
    request_type = getattr(importlib.import_module(input_module), input_name)
    result_type = getattr(importlib.import_module(output_module), output_name)
    assert request_type is ModelGateCheckRequest
    assert result_type is ModelGateCheckResult
    assert request_type is package.ModelGateCheckRequest
    assert result_type is package.ModelGateCheckResult
    assert handler_type is package.HandlerAmbiguityGateDefault
    assert handler_type is HandlerAmbiguityGateDefault
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
    request = ModelGateCheckRequest(
        unit_id="unit-1",
        unit_title="Fix the login bug",
        unit_description="Fix the login failure and add a regression test.",
        unit_type="BUG_FIX",
        dag_id="dag-1",
        intent_id="intent-1",
        correlation_id=correlation_id,
    )
    handler = handler_type()
    result = handler.handle(request)
    assert isinstance(result, ModelGateCheckResult)
    assert result.verdict is EnumGateVerdict.PASS
    assert result.ambiguity_flags == ()
    assert result.unit_id == request.unit_id
    assert result.dag_id == request.dag_id
    assert result.intent_id == request.intent_id

    ambiguous_request = request.model_copy(update={"unit_title": "Fix"})
    with pytest.raises(AmbiguityGateError) as exc_info:
        handler.handle(ambiguous_request)
    rejected = exc_info.value.result
    assert isinstance(rejected, ModelGateCheckResult)
    assert rejected.verdict is EnumGateVerdict.FAIL
    assert rejected.unit_id == request.unit_id
    assert rejected.dag_id == request.dag_id
    assert rejected.intent_id == request.intent_id
    assert (
        rejected.ambiguity_flags[0].ambiguity_type is EnumAmbiguityType.TITLE_TOO_VAGUE
    )
    assert rejected.ambiguity_flags[0].suggested_resolution
    assert handler.handler_type == "node_handler"
    assert handler.handler_category == "compute"
    assert handler.handler_key == "default"
