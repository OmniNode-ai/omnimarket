# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor fill nodes take their shared models from one package (OMN-22931).

The candidates compute, the lines compute, the reducer and the orchestrator share
the models in ``omnimarket.models.floor_fill``. ``test_no_cross_node_reach_in`` reads
only an import line that names ``models``, so a model imported through a sibling
node's ``handlers`` package passes it. This test reads every import in the floor fill
nodes and refuses a shared model that comes from a sibling node, whatever the path.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import omnimarket.models.floor_fill as floor_fill_models

pytestmark = pytest.mark.unit

_NODES_ROOT = Path(__file__).resolve().parent.parent / "src" / "omnimarket" / "nodes"
_PACKAGE = "omnimarket.models.floor_fill"
_NODE_PREFIX = "omnimarket.nodes."
_SHARED = frozenset(floor_fill_models.__all__)


def _violations(source: str, owning_node: str) -> list[str]:
    """Every import in ``source`` that takes a shared model or a models package from another node."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if not node.module.startswith(_NODE_PREFIX):
            continue
        imported_node = node.module.removeprefix(_NODE_PREFIX).split(".")[0]
        if imported_node == owning_node:
            continue
        parts = node.module.split(".")
        if "models" in parts:
            found.append(
                f"from {node.module} import {', '.join(a.name for a in node.names)}"
            )
            continue
        found.extend(
            f"from {node.module} import {alias.name}"
            for alias in node.names
            if alias.name in _SHARED
        )
    return found


def _floor_fill_node_files() -> list[tuple[str, Path]]:
    return [
        (node_dir.name, py_file)
        for node_dir in sorted(_NODES_ROOT.glob("node_floor_fill_*"))
        for py_file in sorted(node_dir.rglob("*.py"))
    ]


def test_the_package_holds_the_models_of_the_three_nodes() -> None:
    assert {
        "ModelFloorFillCandidatesRequest",
        "ModelFloorFillLinesRequest",
        "ModelFloorFillReduceInput",
        "EnumFloorFillEntryState",
        "EnumFloorFillLinesVerdict",
    } <= _SHARED


def test_every_class_and_function_of_the_package_is_defined_in_it() -> None:
    for name in sorted(_SHARED):
        member = getattr(floor_fill_models, name)
        if not (inspect.isclass(member) or inspect.isfunction(member)):
            continue
        assert member.__module__.startswith(_PACKAGE + "."), (
            f"{name} is defined in {member.__module__}"
        )


def test_no_floor_fill_node_takes_a_shared_model_from_a_sibling_node() -> None:
    offenders = [
        f"{py_file.relative_to(_NODES_ROOT)}: {line}"
        for owning_node, py_file in _floor_fill_node_files()
        for line in _violations(py_file.read_text(encoding="utf-8"), owning_node)
    ]
    assert offenders == [], "\n".join(offenders)


def test_the_cross_node_allowlist_names_no_floor_fill_node() -> None:
    allowlist = (
        Path(__file__).resolve().parent / "test_no_cross_node_reach_in.py"
    ).read_text(encoding="utf-8")
    assert "node_floor_fill" not in allowlist
    assert "models.floor_fill" not in allowlist


def test_control_a_model_imported_through_a_sibling_handlers_package_is_found() -> None:
    source = (
        "from omnimarket.nodes.node_floor_fill_candidates_compute.handlers import (\n"
        "    ModelFloorFillCandidatesRequest,\n"
        ")\n"
    )
    assert _violations(source, "node_floor_fill_orchestrator") == [
        "from omnimarket.nodes.node_floor_fill_candidates_compute.handlers import "
        "ModelFloorFillCandidatesRequest"
    ]


def test_control_a_model_imported_from_a_sibling_models_package_is_found() -> None:
    source = (
        "from omnimarket.nodes.node_floor_fill_reducer.models.model_floor_fill_state import "
        "ModelFloorFillState\n"
    )
    assert len(_violations(source, "node_floor_fill_orchestrator")) == 1


def test_control_a_model_imported_from_the_package_is_clean() -> None:
    source = (
        "from omnimarket.models.floor_fill import ModelFloorFillCandidatesRequest\n"
        "from omnimarket.nodes.node_floor_fill_lines_compute.handlers import HandlerFloorFillLines\n"
    )
    assert _violations(source, "node_floor_fill_orchestrator") == []
