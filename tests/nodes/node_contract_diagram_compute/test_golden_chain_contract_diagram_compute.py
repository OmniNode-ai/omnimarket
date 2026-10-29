# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registered package -> contract -> typed handler -> typed result, for both operations."""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_contract_diagram_compute.models.model_contract_diagram import (
    ModelCheckDocumentDiagramsRequest,
    ModelCheckDocumentDiagramsResult,
    ModelRenderContractDiagramRequest,
    ModelRenderContractDiagramResult,
)
from tests.nodes.node_contract_diagram_compute.conftest import fragment_blocks

pytestmark = pytest.mark.unit

NODE = "node_contract_diagram_compute"


def _routed(operation: str) -> tuple[Any, Any, Any]:
    entries = [e for e in entry_points(group="onex.nodes") if e.name == NODE]
    assert len(entries) == 1
    package = entries[0].load()
    contract = yaml.safe_load(
        Path(str(files(package).joinpath("contract.yaml"))).read_text(encoding="utf-8")
    )
    assert contract["name"] == NODE
    assert contract["node_type"] == "compute"
    entry = next(
        h
        for h in contract["handler_routing"]["handlers"]
        if h["operation"] == operation
    )
    handler_type = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    models = []
    for key in ("input_model", "output_model"):
        module_name, _, class_name = entry[key].rpartition(".")
        models.append(getattr(importlib.import_module(module_name), class_name))
    return models[0], models[1], handler_type()


def test_registered_contract_renders_a_fragment_through_its_typed_handler(
    plan_text: str,
) -> None:
    request_type, result_type, handler = _routed("render_contract_diagram")
    assert request_type is ModelRenderContractDiagramRequest
    assert result_type is ModelRenderContractDiagramResult
    fragment = fragment_blocks(plan_text)["node_slot_assignment_reducer:state_machine"]

    result = handler.handle(request_type(fragment_text=fragment))

    assert isinstance(result, result_type)
    assert result.mermaid.splitlines()[0] == "stateDiagram-v2"


def test_registered_contract_checks_a_document_through_its_typed_handler(
    plan_text: str,
) -> None:
    request_type, result_type, handler = _routed("check_document_diagrams")
    assert request_type is ModelCheckDocumentDiagramsRequest
    assert result_type is ModelCheckDocumentDiagramsResult

    result = handler.handle(request_type(document_text=plan_text))

    assert isinstance(result, result_type)
    assert result.exit_code == 0
    assert result.diagrams_checked == 11
