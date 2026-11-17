# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work ledger graph comes from the rows' own cells, deterministically, with no model."""

from __future__ import annotations

import ast
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.handlers.work_ledger_graph_derive import canonical_graph_bytes
from omnimarket.models.work_ledger_graph import (
    ModelWorkLedgerGraphRequest,
    ModelWorkLedgerGraphRow,
)
from omnimarket.nodes.node_work_ledger_graph_compute import NodeWorkLedgerGraphCompute

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[4] / "src" / "omnimarket"
_GRAPH_MODULES = (
    _SRC / "handlers" / "work_ledger_graph_derive.py",
    _SRC
    / "nodes"
    / "node_work_ledger_graph_compute"
    / "handlers"
    / "handler_work_ledger_graph.py",
    _SRC / "models" / "work_ledger_graph" / "model_work_ledger_graph.py",
)
# Any of these in the derivation's imports would put a model or the network in the loop.
_FORBIDDEN_PREFIXES = (
    "anthropic",
    "openai",
    "litellm",
    "httpx",
    "requests",
    "aiohttp",
    "urllib",
    "socket",
    "omnimarket.inference",
    "omnimarket.delegation",
    "omnimarket.delegated_code_edit",
    "omnimarket.routing",
)


def forbidden_imports(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif (
            isinstance(node, ast.Call) and getattr(node.func, "id", "") == "__import__"
        ):
            names = [
                a.value
                for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]
        found += [
            n
            for n in names
            if n.split(".")[0] in _FORBIDDEN_PREFIXES
            or n.startswith(_FORBIDDEN_PREFIXES)
        ]
    return found


def _row(
    ts: str, kind: str, cells: str, text: str = "free text"
) -> ModelWorkLedgerGraphRow:
    raw = f"{ts} | {kind} | {cells} | {text}"
    return ModelWorkLedgerGraphRow(
        row_id=f"id-{ts}-{kind}",
        row_ts=datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC),
        row_type=kind,
        raw_row=raw,
    )


ROWS = (
    _row("2026-11-17T10:00:00Z", "CLAIM", "lane=a | ticket=OMN-1 | parent=orch"),
    _row(
        "2026-11-17T10:05:00Z",
        "STATUS",
        "lane=a | ticket=OMN-1 | pr=OmniNode-ai/omnimarket#1 | head=aaaaaaaaaaaa1111 | installed=h201:aaaaaaaaaaaa1111 | observed=ran",
    ),
    _row(
        "2026-11-17T10:06:00Z",
        "STATUS",
        "lane=a | ticket=OMN-1 | pr=omnimarket#1 | check=CI Summary,Lint",
    ),
    _row(
        "2026-11-17T10:07:00Z",
        "MSG",
        "from=a | to=landing-controller | pr=omnimarket#1 | head=aaaaaaaaaaaa",
    ),
    _row(
        "2026-11-17T10:08:00Z",
        "TERMINAL",
        "lane=a | ticket=OMN-1 | closes-CLAIM=2026-11-17T10:00:00Z | handed-off=omnimarket#1",
    ),
    _row(
        "2026-11-17T10:09:00Z",
        "RULING",
        "lane=orch | kind=process | mechanism-ticket=OMN-9",
        "the text names omnimarket#77 and OMN-555",
    ),
    _row(
        "2026-11-17T10:10:00Z", "STATUS", "lane=c | ticket=OMN-3 | parent_ticket=OMN-2"
    ),
)


def _derive(rows: tuple[ModelWorkLedgerGraphRow, ...]) -> bytes:
    graph = NodeWorkLedgerGraphCompute().handle(ModelWorkLedgerGraphRequest(rows=rows))
    return canonical_graph_bytes(graph.nodes, graph.edges, graph.row_count)


def test_two_runs_and_any_row_order_give_byte_identical_output() -> None:
    first = _derive(ROWS)
    assert _derive(ROWS) == first
    shuffled = list(ROWS)
    random.Random(7).shuffle(shuffled)
    assert _derive(tuple(shuffled)) == first
    graph = NodeWorkLedgerGraphCompute().handle(ModelWorkLedgerGraphRequest(rows=ROWS))
    again = NodeWorkLedgerGraphCompute().handle(
        ModelWorkLedgerGraphRequest(rows=tuple(reversed(ROWS)))
    )
    assert graph.model_dump_json() == again.model_dump_json()
    assert graph.digest == again.digest


def test_edges_come_from_cells_and_never_from_free_text() -> None:
    graph = NodeWorkLedgerGraphCompute().handle(ModelWorkLedgerGraphRequest(rows=ROWS))
    edges = {(e.src, e.rel, e.dst) for e in graph.edges}
    assert ("pr:omnimarket#1", "AT_HEAD", "head:aaaaaaaaaaaa") in edges
    assert ("head:aaaaaaaaaaaa", "INSTALLED_AS", "install:h201:aaaaaaaaaaaa") in edges
    assert ("lane:a", "CHILD_OF", "lane:orch") in edges
    assert ("ticket:OMN-3", "CHILD_OF", "ticket:OMN-2") in edges
    assert ("check:CI Summary", "HOLDS_PR", "pr:omnimarket#1") in edges
    assert ("check:Lint", "HOLDS_PR", "pr:omnimarket#1") in edges
    assert (
        "row:id-2026-11-17T10:08:00Z-TERMINAL",
        "CLOSES",
        "row:id-2026-11-17T10:00:00Z-CLAIM",
    ) in edges
    assert (
        "row:id-2026-11-17T10:08:00Z-TERMINAL",
        "HANDS_OFF",
        "pr:omnimarket#1",
    ) in edges
    assert (
        "row:id-2026-11-17T10:07:00Z-MSG",
        "MSG_TO",
        "lane:landing-controller",
    ) in edges
    assert ("row:id-2026-11-17T10:09:00Z-RULING", "MECHANISM", "ticket:OMN-9") in edges
    node_ids = {n.node_id for n in graph.nodes}
    assert "pr:omnimarket#77" not in node_ids
    assert "ticket:OMN-555" not in node_ids


def test_derivation_imports_no_model_or_network_client() -> None:
    for module in _GRAPH_MODULES:
        assert forbidden_imports(module.read_text(encoding="utf-8")) == [], module


@pytest.mark.parametrize(
    "planted",
    [
        "import anthropic\n",
        "from openai import OpenAI\n",
        "from omnimarket.inference import client\n",
        "import httpx\n",
        "m = __import__('litellm')\n",
    ],
)
def test_no_model_check_goes_red_on_a_planted_model_call(planted: str) -> None:
    source = (_GRAPH_MODULES[0]).read_text(encoding="utf-8") + "\n" + planted
    assert forbidden_imports(source) != []
