# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 and AC13 (static part): the T4 nodes declare topics and overlay keys, resolve handlers, hold no literal (OMN-22263)."""

from __future__ import annotations

import ast
import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODES = (
    "node_route_line_parse_compute",
    "node_work_classify_compute",
    "node_work_intent_classify_effect",
)
OVERLAY_KEYS = {
    "work_type_rubric",
    "run_style_rules",
    "routing_contract_path",
    "delegation_route",
    "route_policy",
    "lab_hosts",
    "timeout_s",
    "precision_bar",
}
ALLOWED_INTS = {0, 1, -1}


def _contract(node: str) -> dict[str, Any]:
    path = REPO / "src" / "omnimarket" / "nodes" / node / "contract.yaml"
    loaded: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


@pytest.mark.parametrize("node", NODES)
def test_the_contract_declares_its_command_and_terminal_event(node: str) -> None:
    contract = _contract(node)
    command = contract["runtime_dispatch"]["command_topic"]
    event = contract["terminal_event"]
    assert contract["event_bus"]["subscribe_topics"] == [command]
    assert contract["event_bus"]["publish_topics"] == [event]
    assert contract["runtime_dispatch"]["terminal_events"]["success"] == event


@pytest.mark.parametrize("node", NODES)
def test_every_routed_handler_resolves_to_a_class_with_handle(node: str) -> None:
    for entry in _contract(node)["handler_routing"]["handlers"]:
        handler = entry["handler"]
        cls = getattr(importlib.import_module(handler["module"]), handler["name"])
        assert callable(cls.handle)
        model = entry["event_model"]
        assert hasattr(importlib.import_module(model["module"]), model["name"])


@pytest.mark.parametrize("node", NODES)
def test_the_node_is_registered_as_an_onex_node_entry_point(node: str) -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert (
        pyproject["project"]["entry-points"]["onex.nodes"][node]
        == f"omnimarket.nodes.{node}"
    )


def test_the_eight_dependencies_are_declared_as_overlay_keys_across_the_nodes() -> None:
    declared: set[str] = set()
    for node in NODES:
        declared |= set(_contract(node)["overlay_keys"])
    assert declared == OVERLAY_KEYS


@pytest.mark.parametrize("node", NODES)
def test_the_node_package_imports_no_event_envelope_and_hardcodes_no_topic(
    node: str,
) -> None:
    for path in (REPO / "src" / "omnimarket" / "nodes" / node).rglob("*.py"):
        body = path.read_text(encoding="utf-8")
        assert "ModelEventEnvelope" not in body, path
        assert "onex.cmd." not in body, path
        assert "onex.evt." not in body, path


def literal_findings(source: str) -> list[str]:
    """Numbers other than the allowed integers and collections of string constants."""
    findings: list[str] = []
    tree = ast.parse(source)
    exports = {
        id(child)
        for stmt in ast.walk(tree)
        if isinstance(stmt, (ast.Assign, ast.AnnAssign))
        and any(isinstance(t, ast.Name) and t.id == "__all__" for t in ast.walk(stmt))
        for child in ast.walk(stmt)
    }
    for node in ast.walk(tree):
        if id(node) in exports:
            continue
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)
            and node.value not in ALLOWED_INTS
        ):
            findings.append(f"number {node.value!r} at line {node.lineno}")
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and any(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
        ):
            findings.append(f"string collection at line {node.lineno}")
    return findings


def test_no_number_threshold_or_id_list_appears_in_handler_code() -> None:
    scanned = 0
    for node in NODES:
        for path in (REPO / "src" / "omnimarket" / "nodes" / node / "handlers").glob(
            "*.py"
        ):
            scanned += 1
            assert literal_findings(path.read_text(encoding="utf-8")) == [], path
    assert scanned >= len(NODES)


def test_a_planted_literal_makes_the_scan_report_it() -> None:
    assert literal_findings("bar = 0.8\n") != []
    assert literal_findings("ids = ('build', 'fix')\n") != []
    assert literal_findings("limit = 80\n") != []
    assert literal_findings("first = items[0]\n") == []
