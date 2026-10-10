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

from omnimarket.nodes.node_nl_intent_pipeline import (
    EnumIntentType,
    EnumResolutionPath,
    HandlerNlIntentDefault,
    ModelIntentObject,
    ModelNlParseRequest,
)

pytestmark = pytest.mark.unit


def test_registered_contract_executes_typed_nl_intent_pipeline() -> None:
    entries = [
        entry
        for entry in entry_points(group="onex.nodes")
        if entry.name == "node_nl_intent_pipeline"
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.value == "omnimarket.nodes.node_nl_intent_pipeline"
    package = entry.load()
    contract = yaml.safe_load(
        Path(str(files(package).joinpath("contract.yaml"))).read_text()
    )
    assert contract["name"] == entry.name
    assert contract["node_type"] == "compute"
    assert contract["capabilities"][0]["name"] == "nl.intent.pipeline.compute"
    assert contract["io_operations"][0]["operation"] == "parse_intent"
    assert contract["io_operations"][0]["protocol_method"] == "handle"

    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    input_module, input_name = binding["input_model"].rsplit(".", 1)
    output_module, output_name = binding["output_model"].rsplit(".", 1)
    request_type = getattr(importlib.import_module(input_module), input_name)
    result_type = getattr(importlib.import_module(output_module), output_name)
    assert request_type is ModelNlParseRequest
    assert result_type is ModelIntentObject
    assert request_type is package.ModelNlParseRequest
    assert result_type is package.ModelIntentObject
    assert handler_type is package.HandlerNlIntentDefault
    assert handler_type is HandlerNlIntentDefault
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
    request = ModelNlParseRequest(
        raw_nl="Fix the login bug in OMN-18008",
        correlation_id=correlation_id,
    )
    handler = handler_type()
    result = handler.handle(request)
    assert isinstance(result, ModelIntentObject)
    assert result.intent_id
    assert result.intent_type is EnumIntentType.BUG_FIX
    assert result.resolution_path is EnumResolutionPath.INFERENCE
    assert result.nl_input_hash == ModelIntentObject.hash_nl_input(request.raw_nl)
    assert result.raw_nl_length == len(request.raw_nl)
    assert result.confidence > 0.5
    assert any(entity.value == "OMN-18008" for entity in result.entities)
    assert handler.handler_type == "node_handler"
    assert handler.handler_category == "compute"
    assert handler.handler_key == "default"
