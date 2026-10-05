# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verdict parity for the projection contract checks (OMN-20567, batch row 13).

The golden file records what the original scripts printed and returned over the
corpus in ``projection_contract_check_corpus.py``. The canonical node
(``node_contract_projection_check_compute``) must reproduce every case exactly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ci.projection_contract_check_corpus import CASES
from tests.ci.projection_contract_check_runners import (
    NODE_MODULE,
    REPO_ROOT,
    SCRIPT_FOR_RULE,
    run_node,
    run_script,
)

pytestmark = [pytest.mark.unit]

GOLDEN = json.loads(
    (
        Path(__file__).parent / "golden" / "projection_contract_checks.golden.json"
    ).read_text(encoding="utf-8")
)


def test_golden_covers_every_corpus_case() -> None:
    assert sorted(GOLDEN) == sorted(case.name for case in CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_original_script_matches_golden(case, tmp_path: Path) -> None:
    assert (REPO_ROOT / SCRIPT_FOR_RULE[case.rule]).is_file()
    assert run_script(tmp_path, case) == GOLDEN[case.name]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_node_matches_golden(case, tmp_path: Path) -> None:
    """The canonical node reproduces exit code, stdout, stderr and the rewritten baseline."""
    assert run_node(tmp_path, case) == GOLDEN[case.name]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_node_matches_live_original_script(case, tmp_path: Path) -> None:
    """Same corpus, live: the node and the script it replaces agree on every case."""
    assert run_node(tmp_path / "node", case) == run_script(tmp_path / "script", case)


def _run_in_repo(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.mark.parametrize("rule", sorted(SCRIPT_FOR_RULE))
def test_node_matches_original_script_on_the_real_tree(rule: str) -> None:
    """Over this repository's own tree the node and the script give the same verdict."""
    expected = _run_in_repo([SCRIPT_FOR_RULE[rule]])
    actual = _run_in_repo(["-m", NODE_MODULE, "--rule", rule])
    assert actual == expected
    assert actual[0] == 0, actual
