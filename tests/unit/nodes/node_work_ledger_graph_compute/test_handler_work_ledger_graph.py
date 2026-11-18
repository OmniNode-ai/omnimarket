# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_work_ledger_graph_compute: edges from the rows' own cells, determinism, no model call."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from omnimarket.models.work_ledger_graph.model_work_ledger_graph import (
    EnumLedgerEdgeKind,
    ModelLedgerGraphRow,
    ModelWorkLedgerGraphComputeRequest,
)
from omnimarket.nodes.node_work_ledger_graph_compute.handlers.handler_work_ledger_graph import (
    HandlerWorkLedgerGraph,
    normalize_pr,
    parse_cells,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_work_ledger_graph_compute"
)
# Modules whose import is a model call or a model client.
MODEL_MODULES = (
    "anthropic",
    "openai",
    "httpx",
    "requests",
    "aiohttp",
    "litellm",
    "urllib.request",
    "omnibase_infra.delegation",
    "omnimarket.nodes.node_typed_decision_effect",
)


def _row(n: int, text: str) -> ModelLedgerGraphRow:
    ts, kind = text.split(" | ")[0], text.split(" | ")[1]
    return ModelLedgerGraphRow(row_id=f"r{n}", row_ts=ts, row_type=kind, raw_row=text)


ROWS = (
    _row(
        1,
        "2026-11-17T10:00:00Z | CLAIM | lane=a-1 | ticket=OMN-1 | parent=orch | est ~1 lane-hours | text",
    ),
    _row(
        2,
        "2026-11-17T10:05:00Z | STATUS | lane=a-1 | pr=OmniNode-ai/omnimarket#7 | head=abcdef1234567890 | installed=h201:abc | observed=ran",
    ),
    _row(
        3,
        "2026-11-17T10:06:00Z | STATUS | lane=rev-1 | verdict=CHANGES-NEEDED head=abcdef123456 pr=omnimarket#7 | review text",
    ),
    _row(
        4,
        "2026-11-17T10:07:00Z | CLAIM | lane=cause-1 | cause=cause:x | check=Lint,Tests | prs=omnimarket#7,overlays#9 | text",
    ),
    _row(
        5,
        "2026-11-17T10:09:00Z | TERMINAL | lane=a-1 | closes-CLAIM=2026-11-17T10:00:00Z | outcome=done | text",
    ),
    _row(
        6,
        "2026-11-17T10:10:00Z | MSG | from=a-1 | to=landing-controller | pr=omnimarket#7 | text",
    ),
)


def test_cells_are_the_rows_own_key_value_cells() -> None:
    cells = parse_cells(ROWS[1].raw_row)
    assert [(c.key, c.value) for c in cells][:3] == [
        ("lane", "a-1"),
        ("pr", "OmniNode-ai/omnimarket#7"),
        ("head", "abcdef1234567890"),
    ]
    # Free text never becomes a cell.
    assert all(c.key != "text" for c in parse_cells(ROWS[0].raw_row))


def test_space_joined_cells_split_only_when_every_token_is_a_cell() -> None:
    keys = [c.key for c in parse_cells(ROWS[2].raw_row)]
    assert keys == ["lane", "verdict", "head", "pr"]
    mixed = parse_cells(
        "2026-11-17T10:00:00Z | STATUS | cause=Dependency scan red shared=yes"
    )
    assert [(c.key, c.value) for c in mixed] == [
        ("cause", "Dependency scan red shared=yes")
    ]


def test_pr_names_normalise() -> None:
    assert normalize_pr("OmniNode-ai/omnimarket#7") == "omnimarket#7"
    assert normalize_pr("omnimarket#7") == "omnimarket#7"
    assert normalize_pr("not a pr") is None


def test_edges_come_from_cells() -> None:
    graph = HandlerWorkLedgerGraph().handle(
        ModelWorkLedgerGraphComputeRequest(rows=ROWS)
    )
    edges = {(e.row_id, e.kind, e.target) for e in graph.edges}
    assert (
        "r1",
        EnumLedgerEdgeKind.OPENS_CLAIM,
        "claim:a-1@2026-11-17T10:00:00Z",
    ) in edges
    assert (
        "r5",
        EnumLedgerEdgeKind.CLOSES_CLAIM,
        "claim:a-1@2026-11-17T10:00:00Z",
    ) in edges
    assert ("r1", EnumLedgerEdgeKind.PARENT_LANE, "lane:orch") in edges
    assert ("r2", EnumLedgerEdgeKind.NAMES_PR, "pr:omnimarket#7") in edges
    assert ("r4", EnumLedgerEdgeKind.NAMES_CHECK, "check:Lint") in edges
    assert ("r4", EnumLedgerEdgeKind.NAMES_CHECK, "check:Tests") in edges
    assert ("r4", EnumLedgerEdgeKind.NAMES_PR, "pr:overlays#9") in edges
    assert ("r6", EnumLedgerEdgeKind.MSG_TO, "lane:landing-controller") in edges
    assert graph.row_count == 6


def test_two_runs_over_the_same_rows_are_byte_identical() -> None:
    first = HandlerWorkLedgerGraph().handle(
        ModelWorkLedgerGraphComputeRequest(rows=ROWS)
    )
    second = HandlerWorkLedgerGraph().handle(
        ModelWorkLedgerGraphComputeRequest(rows=tuple(reversed(ROWS)))
    )
    assert first.model_dump_json() == second.model_dump_json()
    assert len(first.content_sha256) == 64


def _imports(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _model_imports(source: str) -> set[str]:
    return {
        name
        for name in _imports(source)
        for mod in MODEL_MODULES
        if name == mod or name.startswith(mod + ".")
    }


def test_the_node_imports_no_model_client() -> None:
    sources = sorted(NODE_DIR.rglob("*.py"))
    assert sources, "positive control: the node has python sources"
    for path in sources:
        assert _model_imports(path.read_text(encoding="utf-8")) == set(), path


def test_the_no_model_check_is_red_on_a_planted_model_call() -> None:
    source = (NODE_DIR / "handlers" / "handler_work_ledger_graph.py").read_text(
        encoding="utf-8"
    )
    planted = (
        source
        + "\n\nimport anthropic\n\ndef _extract(text: str) -> object:\n    return anthropic.Anthropic().messages.create(model='x', max_tokens=1, messages=[])\n"
    )
    assert _model_imports(planted) == {"anthropic"}


def test_contract_publishes_the_graph_computed_terminal_event() -> None:
    import yaml

    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    topic = "onex.evt.omnimarket.work-ledger-graph-computed.v1"
    assert contract["terminal_event"] == topic
    assert contract["event_bus"]["publish_topics"] == [topic]
