# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's contract, its configuration (none) and its bindings (none)."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, JsonValue

from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_run_local import (
    run_local,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_run_local import (
    ModelRunLocalConfig,
)
from omnimarket.nodes.node_contract_diagram_compute.handlers.handler_check_document_diagrams import (
    HandlerCheckDocumentDiagrams,
)
from omnimarket.nodes.node_contract_diagram_compute.handlers.handler_render_contract_diagram import (
    HandlerRenderContractDiagram,
)
from omnimarket.nodes.node_contract_diagram_compute.models.model_contract_diagram import (
    ModelCheckDocumentDiagramsRequest,
    ModelRenderContractDiagramRequest,
    ModelRenderContractDiagramResult,
)
from tests.nodes.node_contract_diagram_compute.conftest import fragment_blocks

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_contract_diagram_compute"
PARITY_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_binding_parity_orchestrator"
NODE = "node_contract_diagram_compute"


def contract() -> dict[str, Any]:
    document = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_the_contract_routes_both_operations_to_their_handlers() -> None:
    handlers = {h["operation"]: h for h in contract()["handler_routing"]["handlers"]}
    assert set(handlers) == {"render_contract_diagram", "check_document_diagrams"}
    assert (
        handlers["render_contract_diagram"]["handler"]["name"]
        == "HandlerRenderContractDiagram"
    )
    assert (
        handlers["check_document_diagrams"]["handler"]["name"]
        == "HandlerCheckDocumentDiagrams"
    )
    assert contract()["node_type"] == "compute"


def test_the_contract_publishes_one_completion_event_and_listens_on_one_topic_per_operation() -> (
    None
):
    document = contract()
    completed = "onex.evt.omnimarket.contract-diagram-completed.v1"
    assert document["terminal_event"] == completed
    assert document["event_bus"]["publish_topics"] == [completed]
    handlers = document["handler_routing"]["handlers"]
    assert [h["subscribe_topic"] for h in handlers] == document["event_bus"][
        "subscribe_topics"
    ]
    assert len(set(document["event_bus"]["subscribe_topics"])) == 2


def test_the_node_is_registered_under_onex_nodes() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entries = pyproject["project"]["entry-points"]["onex.nodes"]
    assert entries[NODE] == f"omnimarket.nodes.{NODE}"
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["entry_points"]["onex.nodes"][NODE] == f"omnimarket.nodes.{NODE}"


# Configuration: no configured value -----------------------------------------------------------


def test_the_request_models_take_the_text_and_the_view_and_nothing_else() -> None:
    assert set(ModelRenderContractDiagramRequest.model_fields) == {
        "operation",
        "fragment_text",
        "view",
    }
    assert set(ModelCheckDocumentDiagramsRequest.model_fields) == {
        "operation",
        "document_text",
    }
    with pytest.raises(ValueError, match="extra"):
        ModelRenderContractDiagramRequest.model_validate(
            {"fragment_text": "x", "option": 1}
        )
    with pytest.raises(ValueError, match="extra"):
        ModelCheckDocumentDiagramsRequest.model_validate(
            {"document_text": "x", "strict": True}
        )


def test_the_node_source_reads_no_environment_argument_file_or_clock() -> None:
    banned_names = {
        "environ",
        "getenv",
        "argv",
        "open",
        "read_text",
        "read_bytes",
        "now",
        "utcnow",
    }
    banned_modules = {
        "os",
        "sys",
        "subprocess",
        "pathlib",
        "argparse",
        "socket",
        "datetime",
        "time",
    }
    sources = list(NODE_DIR.rglob("*.py"))
    assert sources
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert (
                    not {a.name.split(".")[0] for a in node.names} & banned_modules
                ), source
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned_modules, source
            if isinstance(node, ast.Attribute):
                assert node.attr not in banned_names, (source, node.attr)
            if isinstance(node, ast.Name):
                assert node.id not in banned_names, (source, node.id)


def test_the_result_does_not_move_with_the_environment(
    plan_text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fragment = fragment_blocks(plan_text)["node_wip_gate_reducer:state_machine"]
    request = ModelRenderContractDiagramRequest(fragment_text=fragment)
    before = HandlerRenderContractDiagram().handle(request)
    for name in list(os.environ):
        if name.startswith(("ONEX", "OMNI", "MERMAID", "DIAGRAM")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", "/nowhere")
    monkeypatch.setenv("MERMAID_VIEW", "collapsed")
    assert HandlerRenderContractDiagram().handle(request) == before


# Bindings: no protocol; the same bytes in process and through the local in-process entry -------


def test_the_contract_declares_no_protocol_dependency() -> None:
    document = contract()
    assert "protocol_dependencies" not in document
    assert not any("protocol" in key for key in document)


def run_config() -> ModelRunLocalConfig:
    parity = yaml.safe_load((PARITY_DIR / "contract.yaml").read_text(encoding="utf-8"))
    return ModelRunLocalConfig.model_validate(parity["config"]["run_local"])


def run_through_entry(
    operation: str, payload: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    return asyncio.run(run_local(NODE, operation, payload, run_config()))


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def test_render_is_byte_identical_in_process_and_through_the_local_entry(
    plan_text: str,
) -> None:
    for key, fragment in fragment_blocks(plan_text).items():
        request = ModelRenderContractDiagramRequest(fragment_text=fragment)
        in_process = HandlerRenderContractDiagram().handle(request)
        through_entry = run_through_entry(
            "render_contract_diagram", request.model_dump()
        )
        assert canonical(through_entry) == canonical(
            json.loads(in_process.model_dump_json())
        ), key


def test_check_is_byte_identical_in_process_and_through_the_local_entry(
    plan_text: str,
) -> None:
    drifted = plan_text.replace(
        "fixed --> open : readback_mismatch", "fixed --> stale : readback_mismatch"
    )
    for text in (plan_text, drifted):
        request = ModelCheckDocumentDiagramsRequest(document_text=text)
        in_process = HandlerCheckDocumentDiagrams().handle(request)
        through_entry = run_through_entry(
            "check_document_diagrams", request.model_dump()
        )
        assert canonical(through_entry) == canonical(
            json.loads(in_process.model_dump_json())
        )


def test_a_stub_that_changes_the_behaviour_makes_the_comparison_fail(
    plan_text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_binding_parity_orchestrator.handlers import (
        handler_run_local,
    )

    fragment = fragment_blocks(plan_text)["node_wip_gate_reducer:state_machine"]
    request = ModelRenderContractDiagramRequest(fragment_text=fragment)
    in_process = HandlerRenderContractDiagram().handle(request)

    real = handler_run_local.resolve_operation

    def altered(
        node_name: str, operation: str
    ) -> tuple[type[BaseModel], Callable[[BaseModel], object]]:
        input_model, handle = real(node_name, operation)

        def handle_with_a_dropped_line(
            payload: BaseModel,
        ) -> ModelRenderContractDiagramResult:
            result = handle(payload)
            assert isinstance(result, ModelRenderContractDiagramResult)
            lines = result.mermaid.splitlines()
            return result.model_copy(update={"mermaid": "\n".join(lines[:-1]) + "\n"})

        return input_model, handle_with_a_dropped_line

    monkeypatch.setattr(handler_run_local, "resolve_operation", altered)
    through_entry = run_through_entry("render_contract_diagram", request.model_dump())

    assert canonical(through_entry) != canonical(
        json.loads(in_process.model_dump_json())
    )
