# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""
OMN-26129 AC2, whole: no packaged routing, placement or judge configuration names the withdrawn rung, and the tests that pinned it name the new one.

The checks and their planted controls moved here from test_cheap_frontier_closeout_omn26129.py so that one file covers the whole criterion, and the helpers stay in that module and are imported from it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_delegation_answer_gate_compute.handlers.judge_selection import (
    answer_gate_contract,
)
from tests.unit.routing.test_cheap_frontier_closeout_omn26129 import (
    _PLACEMENT,
    _ROUTING,
    JUDGE_CONFIGS,
    NEW_MODEL,
    NEW_RUNG,
    PINNING_TESTS,
    PINNING_TESTS_KEEPING_THE_OLD_ID,
    WITHDRAWN,
    _ladder,
    _named_withdrawn,
    _packaged_backends,
    _packaged_config_files,
    _pinning_problems,
    _read,
    _withdrawn_judges,
)

pytestmark = pytest.mark.unit


def test_no_packaged_routing_placement_or_judge_config_names_the_withdrawn_rung() -> (
    None
):
    files = _packaged_config_files()
    names = {p.name for p in files}
    assert {_ROUTING, _PLACEMENT} <= set(files)
    assert set(JUDGE_CONFIGS) <= names, (
        f"judge configs not scanned: {set(JUDGE_CONFIGS) - names}"
    )
    assert _named_withdrawn(files) == []


def test_the_scan_catches_a_planted_withdrawn_rung_and_refuses_an_unreadable_file(
    tmp_path: Path,
) -> None:
    planted = tmp_path / "bifrost_delegation.yaml"
    planted.write_text(
        "backends:\n  - backend_id: openrouter-north-mini-code\n"
        "    model_name: cohere/north-mini-code:free\n",
        encoding="utf-8",
    )
    judge = tmp_path / "delegation_independent_judge.v1.yaml"
    judge.write_text("judge_backend: openrouter-north-mini-code\n", encoding="utf-8")
    assert {w for _, w in _named_withdrawn([planted])} == set(WITHDRAWN)
    assert _named_withdrawn([judge]) == [(str(judge), "openrouter-north-mini-code")]
    broken = tmp_path / "broken.yaml"
    broken.write_text("backends: [unclosed\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        _named_withdrawn([broken])


def test_the_judge_never_selects_the_withdrawn_rung() -> None:
    judged, withdrawn = _withdrawn_judges(_packaged_backends())
    assert judged > 0, "no producer family selects a judge: the check would be vacuous"
    assert withdrawn == []


def test_the_judge_check_catches_a_planted_withdrawn_judge() -> None:
    planted = {
        "backend_id": "openrouter-north-mini-code",
        "model_name": "cohere/north-mini-code:free",
        "capabilities": [answer_gate_contract()["judge_capability"]],
    }
    _, withdrawn = _withdrawn_judges([planted, *_packaged_backends()])
    assert withdrawn == sorted(WITHDRAWN)


def test_the_tests_that_pinned_the_old_rung_name_the_new_one() -> None:
    rung = _ladder(_packaged_backends(), "cheap_frontier")
    assert [(b["backend_id"], b["model_name"]) for b in rung] == [(NEW_RUNG, NEW_MODEL)]
    assert _pinning_problems(_read(PINNING_TESTS), must_drop_the_old_id=True) == []
    assert (
        _pinning_problems(
            _read(PINNING_TESTS_KEEPING_THE_OLD_ID), must_drop_the_old_id=False
        )
        == []
    )


def test_the_pinning_check_catches_a_test_still_pinning_the_old_rung() -> None:
    old = {"a.py": '_RUNG = "openrouter-north-mini-code"\n'}
    both = {"b.py": f'_RUNG = "{NEW_RUNG}"\n# was cohere/north-mini-code:free\n'}
    assert _pinning_problems(old, must_drop_the_old_id=False) == [
        f"a.py does not name {NEW_RUNG}"
    ]
    assert _pinning_problems(both, must_drop_the_old_id=True) == [
        "b.py still names cohere/north-mini-code:free"
    ]
