# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verdict parity for the projection contract checks (OMN-20567, batch row 13).

The golden file records what the original scripts printed and returned over the
corpus in ``projection_contract_check_corpus.py``. The canonical node
(``node_projection_contract_check_compute``) must reproduce every case exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ci.projection_contract_check_corpus import CASES
from tests.ci.projection_contract_check_runners import (
    REPO_ROOT,
    SCRIPT_FOR_RULE,
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
