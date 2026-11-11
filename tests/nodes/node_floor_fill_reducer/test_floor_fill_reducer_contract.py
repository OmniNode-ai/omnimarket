# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor fill reducer's tests that read the node's own contract, metadata and handler source.

The core ``no_io_outside_effects`` validator judges every module under a non-effect node
directory, tests included, takes no suppression comment and no baseline, and refuses a file
read. These tests have to read the node's contract (the transition conformance, the walk
and the binding) and its handler source (no clock, no I/O, no constructor), so they live
here, outside the node package, and import the fixture and the helpers from the node's own
tests. ``-k transition_conformance`` collects them from here and from the node's tests.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.validation.validator_fsm_analysis import analyze_fsm

from omnimarket.nodes.node_floor_fill_reducer.handlers.handler_floor_fill_reducer import (
    HandlerFloorFillReducer,
)
from omnimarket.nodes.node_floor_fill_reducer.tests.test_floor_fill_reducer import (
    FIXTURE_ROWS,
    conformance_findings,
    contract_rows,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_floor_fill_reducer"
)
HANDLER_SOURCE = NODE_DIR / "handlers" / "handler_floor_fill_reducer.py"
CONTRACT = NODE_DIR / "contract.yaml"


def load_contract() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_transition_conformance_contract_state_machine_equals_the_fixture() -> None:
    assert conformance_findings(contract_rows(load_contract()), FIXTURE_ROWS) == []


def test_transition_conformance_detects_a_missing_extra_or_differently_guarded_row() -> (
    None
):
    rows = contract_rows(load_contract())
    assert conformance_findings(rows[1:], FIXTURE_ROWS) != []
    extra = [*rows, {**rows[0], "trigger": "invented", "row": "x"}]
    assert any("extra row" in f for f in conformance_findings(extra, FIXTURE_ROWS))
    guarded = copy.deepcopy(rows)
    guarded[1]["guards"] = guarded[1]["guards"][:-1]
    assert any("differs" in f for f in conformance_findings(guarded, FIXTURE_ROWS))


def test_transition_conformance_contract_binds_the_handler_table() -> None:
    contract = load_contract()
    [binding] = contract["fsm_handler_binding"]
    assert binding["handler_module"] == (
        "omnimarket.nodes.node_floor_fill_reducer.handlers.handler_floor_fill_reducer"
    )
    assert binding["valid_transitions_symbol"] == "VALID_TRANSITIONS"
    assert set(binding["state_filter"]) == {r["from"] for r in FIXTURE_ROWS}


def test_transition_conformance_the_machine_walks_clean() -> None:
    fsm = ModelFSMSubcontract.model_validate(load_contract()["state_machine"])
    result = analyze_fsm(fsm)
    assert not result.unreachable_states
    assert not result.dead_transitions
    assert not result.missing_transitions
    assert not result.ambiguous_transitions
    assert not result.duplicate_state_names


def test_seams_in_spi_the_reducer_injects_no_protocol() -> None:
    contract = load_contract()
    assert not contract.get("protocol_dependencies")
    assert HandlerFloorFillReducer().__dict__ == {}
    init = ast.parse(HANDLER_SOURCE.read_text(encoding="utf-8"))
    [handler] = [
        n
        for n in ast.walk(init)
        if isinstance(n, ast.ClassDef) and n.name == "HandlerFloorFillReducer"
    ]
    assert not [
        f
        for f in ast.walk(handler)
        if isinstance(f, ast.FunctionDef) and f.name == "__init__"
    ]


def test_the_node_reads_no_clock_and_does_no_io() -> None:
    tree = ast.parse(HANDLER_SOURCE.read_text(encoding="utf-8"))
    forbidden_modules = {
        "os",
        "sys",
        "time",
        "socket",
        "subprocess",
        "pathlib",
        "random",
        "uuid",
        "requests",
        "httpx",
        "asyncio",
        "logging",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & forbidden_modules
    calls = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert not calls & {
        "now",
        "utcnow",
        "today",
        "time",
        "read_text",
        "write_text",
        "open",
    }
    names = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "open" not in names
    assert "print" not in names


def test_the_contract_loads_and_names_the_node() -> None:
    contract = load_contract()
    assert contract["name"] == "node_floor_fill_reducer"
    assert contract["node_type"] == "REDUCER_GENERIC"
    assert contract["input_model"]["name"] == "ModelFloorFillReduceInput"
    assert contract["output_model"]["name"] == "ModelFloorFillReduceOutput"
    assert contract["descriptor"]["purity"] == "pure"
    assert not contract["event_bus"]["subscribe_topics"]
    assert not contract["event_bus"]["publish_topics"]
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == "node_floor_fill_reducer"
    assert metadata["node_role"] == "reducer"
