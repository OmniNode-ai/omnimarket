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

from omnimarket.nodes.node_ticket_compiler import (
    EnumAssertionType,
    EnumSandboxLevel,
    HandlerTicketCompileDefault,
    ModelCompiledTicket,
    ModelTicketCompileRequest,
)

pytestmark = pytest.mark.unit


def test_registered_contract_executes_typed_ticket_compiler() -> None:
    entries = [
        entry
        for entry in entry_points(group="onex.nodes")
        if entry.name == "node_ticket_compiler"
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.value == "omnimarket.nodes.node_ticket_compiler"
    package = entry.load()
    contract = yaml.safe_load(
        Path(str(files(package).joinpath("contract.yaml"))).read_text()
    )
    assert contract["name"] == entry.name
    assert contract["node_type"] == "compute"
    assert contract["capabilities"][0]["name"] == "nl.ticket.compile.compute"
    assert contract["io_operations"][0]["operation"] == "compile_ticket"
    assert contract["io_operations"][0]["protocol_method"] == "handle"

    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    input_module, input_name = binding["input_model"].rsplit(".", 1)
    output_module, output_name = binding["output_model"].rsplit(".", 1)
    request_type = getattr(importlib.import_module(input_module), input_name)
    result_type = getattr(importlib.import_module(output_module), output_name)
    assert request_type is ModelTicketCompileRequest
    assert result_type is ModelCompiledTicket
    assert request_type is package.ModelTicketCompileRequest
    assert result_type is package.ModelCompiledTicket
    assert handler_type is package.HandlerTicketCompileDefault
    assert handler_type is HandlerTicketCompileDefault
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
    request = ModelTicketCompileRequest(
        work_unit_id="unit-1",
        work_unit_title="Fix the login bug",
        work_unit_description="Fix the login failure and add a regression test.",
        work_unit_type="BUG_FIX",
        dag_id="dag-1",
        intent_id="intent-1",
        correlation_id=correlation_id,
    )
    handler = handler_type()
    result = handler.handle(request)
    assert isinstance(result, ModelCompiledTicket)
    assert result.ticket_id
    assert result.work_unit_id == request.work_unit_id
    assert result.dag_id == request.dag_id
    assert result.intent_id == request.intent_id
    assert result.title == request.work_unit_title
    assert result.idl_spec.input_schema
    assert result.idl_spec.output_schema
    assert len(result.acceptance_criteria) == 2
    assert all(
        ac.assertion_type is EnumAssertionType.TEST_PASSES
        for ac in result.acceptance_criteria
    )
    assert result.policy_envelope.sandbox_level is EnumSandboxLevel.STANDARD
    assert "## Acceptance Criteria" in result.description
    assert handler.handler_type == "node_handler"
    assert handler.handler_category == "compute"
    assert handler.handler_key == "default"
