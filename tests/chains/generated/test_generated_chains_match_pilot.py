# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The generated delegation chains equal the hand-written pilot cases (OMN-19713).

Read from the pilot's source: for every ``chain_obligation`` case, the
``expected_event_types`` list it asserts and, where it pins one, the
``run.states`` tuple. Both must equal the generated chain for that path.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.chains.generated.test_generated_chains import committed

pytestmark = pytest.mark.unit

PILOT = Path(__file__).parents[1] / "delegation"
OWNER = "node_delegation_orchestrator"


def _literal_path_id(node: ast.expr) -> str:
    value = ast.literal_eval(node)
    assert isinstance(value, str)
    return value


def pilot_cases() -> dict[str, tuple[list[str], list[str] | None]]:
    cases: dict[str, tuple[list[str], list[str] | None]] = {}
    for source in sorted(PILOT.glob("test_chain_delegation_*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for fn in tree.body:
            if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            path_ids = [
                _literal_path_id(d.args[0])
                for d in fn.decorator_list
                if isinstance(d, ast.Call)
                and isinstance(d.func, ast.Name)
                and d.func.id == "chain_obligation"
            ]
            if not path_ids:
                continue
            events: list[str] | None = None
            states: list[str] | None = None
            for node in ast.walk(fn):
                if isinstance(node, ast.keyword) and node.arg == "expected_event_types":
                    events = ast.literal_eval(node.value)
                if (
                    isinstance(node, ast.Compare)
                    and isinstance(node.left, ast.Attribute)
                    and node.left.attr == "states"
                    and isinstance(node.comparators[0], ast.Tuple)
                ):
                    states = [
                        elt.attr
                        for elt in node.comparators[0].elts
                        if isinstance(elt, ast.Attribute)
                    ]
            assert events is not None, fn.name
            cases[path_ids[0]] = (events, states)
    return cases


def test_pilot_covers_every_generated_path_and_matches_it() -> None:
    generated = committed(OWNER)
    assert generated is not None, "no generated chains committed for delegation"
    pilot = pilot_cases()
    assert set(pilot) == {c.path_id for c in generated.chains}
    for chain in generated.chains:
        events, states = pilot[chain.path_id]
        assert list(chain.expected_event_types) == events, chain.path_id
        if states is not None:
            assert list(chain.expected_states) == states, chain.path_id
