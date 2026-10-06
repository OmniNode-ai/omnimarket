# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every walker obligation of the PR handoff workflow is bound to exactly one chain case (OMN-20636).

The walker report (validator_contract_walker over src, reduced to the owner
component by tests/chains/delegation/obligations.py) is the denominator, so
coverage is counted against the contract's paths and not against hand-picked
scenarios. A new transition in the contract changes the report, and a path
with no chain case fails here.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest
import yaml

from tests.chains.delegation.obligations import load_obligations

pytestmark = pytest.mark.unit

_HERE = Path(__file__).parent
_REPORT = _HERE / "walker_report.json"
_CONTRACT = (
    _HERE.parents[2] / "src/omnimarket/nodes/node_pr_handoff_orchestrator/contract.yaml"
)


def _bound_path_ids() -> Counter[str]:
    bound: Counter[str] = Counter()
    for path in sorted(_HERE.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "chain_obligation"
                and node.args
            ):
                value = ast.literal_eval(node.args[0])
                assert isinstance(value, str)
                bound[value] += 1
    return bound


def test_every_walker_obligation_is_bound_exactly_once() -> None:
    obligations = {o.path_id for o in load_obligations(_REPORT)}
    bound = _bound_path_ids()

    assert len(obligations) == 13
    assert set(bound) - obligations == set(), "a marker names an unknown path id"
    assert obligations - set(bound) == set(), "an obligation has no chain case"
    assert {path_id: n for path_id, n in bound.items() if n != 1} == {}


def test_walker_report_matches_the_contract_transitions() -> None:
    """The committed report was reduced from this contract: every projected step is a declared edge."""
    machine = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))["state_machine"]
    declared = {
        (t["from_state"], t["trigger"], t["to_state"]) for t in machine["transitions"]
    }
    stepped = {step for o in load_obligations(_REPORT) for step in o.steps}
    assert stepped <= declared
    changing = {edge for edge in declared if edge[0] != edge[2]}
    assert changing == stepped, "a state-changing transition is on no walker path"
