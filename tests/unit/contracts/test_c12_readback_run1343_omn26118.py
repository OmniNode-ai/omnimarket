# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The newer C12 readback (run 1343) holds all three criteria; known bad records are refused."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit.contracts.test_c12_readback_omn26118 import refusals

RECORD = (
    Path(__file__).resolve().parents[3]
    / "contracts/evidence/OMN-26118/c12-readback-run1343.json"
)


@pytest.fixture(scope="module")
def record() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(RECORD.read_text())
    return loaded


@pytest.mark.unit
def test_run_1343_record_holds_all_three_criteria(record: dict[str, Any]) -> None:
    assert refusals(record) == []
    green = record["ac2_green_runs"]
    assert green["newest_run_id"] == 45905096364
    assert green["newest_run_number"] == 1343
    assert green["run_numbers_success"] == [1339, 1340, 1341, 1342, 1343]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (("ac2_green_runs", "conclusion"), "failure", "AC2"),
        (("ac2_green_runs", "newest_run_number"), 1344, "AC2"),
        (("ac2_clause_table", "removed_assertions"), ["no_house_rung"], "AC2"),
        (("ac3_status_row", "verdict"), "red", "AC3"),
        (("ac1_cause", "newest_failure_text"), [], "AC1"),
    ],
)
def test_known_bad_records_are_refused(
    record: dict[str, Any], path: tuple[str, ...], value: Any, expected: str
) -> None:
    bad = copy.deepcopy(record)
    node: Any = bad
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert any(line.startswith(expected) for line in refusals(bad))
