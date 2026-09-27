# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every walker obligation of the delegation workflow is bound to exactly one chain case.

This is pilot measurement M3 of the golden-chain plan (OMN-19713): the walker
report is the denominator, so coverage is counted against the workflow's paths
and not against hand-picked scenarios.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from tests.chains.delegation.obligations import load_obligations

pytestmark = pytest.mark.unit


def _bound_path_ids() -> Counter[str]:
    bound: Counter[str] = Counter()
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
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
    obligations = {obligation.path_id for obligation in load_obligations()}
    bound = _bound_path_ids()

    assert len(obligations) == 11
    assert set(bound) - obligations == set(), "a marker names an unknown path id"
    assert obligations - set(bound) == set(), "an obligation has no chain case"
    assert {path_id: n for path_id, n in bound.items() if n != 1} == {}
