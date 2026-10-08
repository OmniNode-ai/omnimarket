# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registered package -> contract -> typed handler -> typed result."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from importlib.metadata import entry_points
from importlib.resources import files
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_evidence_bundle import (
    EnumAcVerdict,
    EnumExecutionOutcome,
    HandlerEvidenceBundleDefault,
    ModelAcVerificationRecord,
    ModelBundleGenerateRequest,
    ModelEvidenceBundle,
    StoreBundleInMemory,
)

pytestmark = pytest.mark.unit


def test_registered_contract_executes_typed_evidence_bundle() -> None:
    entries = [
        entry
        for entry in entry_points(group="onex.nodes")
        if entry.name == "node_evidence_bundle"
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.value == "omnimarket.nodes.node_evidence_bundle"
    package = entry.load()
    contract = yaml.safe_load(
        Path(str(files(package).joinpath("contract.yaml"))).read_text()
    )
    assert contract["name"] == entry.name
    assert contract["node_type"] == "compute"
    assert contract["capabilities"][0]["name"] == "nl.evidence.bundle.compute"
    assert contract["io_operations"][0]["operation"] == "generate"
    assert contract["io_operations"][0]["protocol_method"] == "handle"

    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    input_module, input_name = binding["input_model"].rsplit(".", 1)
    output_module, output_name = binding["output_model"].rsplit(".", 1)
    request_type = getattr(importlib.import_module(input_module), input_name)
    result_type = getattr(importlib.import_module(output_module), output_name)
    assert request_type is ModelBundleGenerateRequest
    assert result_type is ModelEvidenceBundle
    assert request_type is package.ModelBundleGenerateRequest
    assert result_type is package.ModelEvidenceBundle
    assert handler_type is package.HandlerEvidenceBundleDefault
    assert handler_type is HandlerEvidenceBundleDefault
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
    started_at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    completed_at = datetime(2026, 1, 1, 10, 5, tzinfo=UTC)
    record = ModelAcVerificationRecord(
        criterion_id="unit-1-tests",
        verdict=EnumAcVerdict.PASS,
        actual_value="0",
        verified_at=completed_at,
    )
    request = ModelBundleGenerateRequest(
        ticket_id="ticket-1",
        work_unit_id="unit-1",
        dag_id="dag-1",
        intent_id="intent-1",
        nl_input_hash="a" * 64,
        outcome=EnumExecutionOutcome.SUCCESS,
        ac_records=(record,),
        actual_outputs=(("tests_passed", "1"),),
        started_at=started_at,
        completed_at=completed_at,
        correlation_id=correlation_id,
    )
    store = StoreBundleInMemory()
    handler = handler_type(store=store)
    result = handler.handle(request)
    assert isinstance(result, ModelEvidenceBundle)
    assert result.bundle_id
    assert result.ticket_id == request.ticket_id
    assert result.work_unit_id == request.work_unit_id
    assert result.dag_id == request.dag_id
    assert result.intent_id == request.intent_id
    assert result.nl_input_hash == request.nl_input_hash
    assert result.outcome is EnumExecutionOutcome.SUCCESS
    assert result.ac_records == (record,)
    assert result.actual_outputs == request.actual_outputs
    assert result.started_at == started_at
    assert result.completed_at == completed_at
    assert store.get(result.bundle_id) is result
    assert store.get_by_ticket_id(request.ticket_id) is result
    assert handler.handler_type == "node_handler"
    assert handler.handler_category == "compute"
    assert handler.handler_key == "default"
