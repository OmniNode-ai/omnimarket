# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23138: the count of dispatch importing tickets-plans handlers, read from this tree.

The labels follow the plan's section 4.1 table row "dispatch to tickets-plans": lane intake is
the dispatch side and the ticket nodes' handler packages are the tickets-plans side. The count
comes from the same relation node the graph gate uses, run over the import edges found in the
source tree by an ast walk. The planted-import control shows the same walk reads one when the
import is present.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from omnimarket.models.import_relations import (
    ModelImportEdge,
    ModelImportRelationRequest,
    ModelLabelRule,
    ModelRelationKey,
)
from omnimarket.nodes.node_network_graph_compute.handlers.handler_import_relation_compute import (
    HandlerImportRelationCompute,
)
from tests.unit.nodes.node_network_graph_compute.relation_support import (
    REFERENCE_TIME,
    example_overlay,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[4]
_SRC = _ROOT / "src"
_RELATION = ModelRelationKey(source="dispatch", target="tickets-plans")
_RULES = (
    ModelLabelRule(label="dispatch", repo="omnimarket", path_prefix="src/omnimarket/handlers/handler_lane_intake"),
    ModelLabelRule(label="tickets-plans", repo="omnimarket", path_prefix="src/omnimarket/nodes/node_create_ticket/handlers/"),
    ModelLabelRule(label="tickets-plans", repo="omnimarket", path_prefix="src/omnimarket/nodes/node_linear_ticket_write_effect/handlers/"),
)  # fmt: skip


def _module_path(dotted: str) -> str | None:
    """The file a dotted module name lands on in this tree, else None."""
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        base = _SRC.joinpath(*parts[:cut])
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            if candidate.is_file():
                return candidate.relative_to(_ROOT).as_posix()
    return None


def edges_of(source: str, importer_path: str) -> list[ModelImportEdge]:
    """Every first-party import in ``source`` (top level, nested and lazy) as an edge."""
    found: list[ModelImportEdge] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        else:
            continue
        landed = {
            p for n in names if n.startswith("omnimarket") and (p := _module_path(n))
        }
        found.extend(
            ModelImportEdge(
                importer_repo="omnimarket",
                importer_path=importer_path,
                imported_repo="omnimarket",
                imported_path=path,
            )
            for path in sorted(landed)
        )
    return found


def tree_edges() -> list[ModelImportEdge]:
    edges: list[ModelImportEdge] = []
    for path in sorted((_SRC / "omnimarket").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        edges.extend(edges_of(path.read_text(encoding="utf-8"), rel))
    return edges


def count_of_relation(edges: list[ModelImportEdge]) -> int:
    overlay = example_overlay().model_copy(
        update={"label_rules": _RULES, "tracked_relations": (_RELATION,)}
    )
    report = HandlerImportRelationCompute().handle(
        ModelImportRelationRequest(
            edges=tuple(edges), overlay=overlay, reference_time=REFERENCE_TIME
        )
    )
    return next((r.occurrences for r in report.relations if r.key == _RELATION), 0)


def test_relation_count_dispatch_tickets_plans_reads_zero_in_this_tree() -> None:
    assert count_of_relation(tree_edges()) == 0


def test_relation_count_dispatch_tickets_plans_control_reads_one_for_a_planted_import() -> (
    None
):
    planted = (
        "def guard():\n"
        "    from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (\n"
        "        locate_ticket_guard,\n"
        "    )\n"
        "    return locate_ticket_guard()\n"
    )
    edges = edges_of(planted, "src/omnimarket/handlers/handler_lane_intake.py")
    assert count_of_relation(edges) == 1


def test_relation_count_dispatch_tickets_plans_the_create_module_is_on_the_dispatch_side() -> (
    None
):
    """The module that creates by command carries the dispatch label, so the zero covers it."""
    path = "src/omnimarket/handlers/handler_lane_intake_create.py"
    assert (_ROOT / path).is_file()
    own = [e for e in tree_edges() if e.importer_path == path]
    assert own, "the create module imports nothing: the walk is not reading it"
    assert all(
        not e.imported_path.startswith(rule.path_prefix)
        for e in own
        for rule in _RULES
        if rule.label == "tickets-plans"
    )
