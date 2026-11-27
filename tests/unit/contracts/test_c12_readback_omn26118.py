# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The committed C12 readback names the red's cause and holds the green reading its contract binds."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

RECORD = (
    Path(__file__).resolve().parents[3]
    / "contracts/evidence/OMN-26118/c12-readback.json"
)


def refusals(record: dict[str, Any]) -> list[str]:
    """Name each criterion the record fails to hold; an empty list means it holds all three."""
    found: list[str] = []
    cause = record.get("ac1_cause", {})
    failures = cause.get("newest_failure_text", [])
    if not cause.get("summary") or not failures:
        found.append("AC1: no cause or no failure text")
    for failure in failures:
        if not isinstance(failure.get("run_id"), int) or not failure.get("line"):
            found.append("AC1: a failure line carries no run id or no text")
    green = record.get("ac2_green_runs", {})
    runs = green.get("run_numbers_success", [])
    if (
        green.get("conclusion") != "success"
        or green.get("newest_run_number") not in runs
    ):
        found.append("AC2: the newest run does not read success")
    if not runs or runs != sorted(runs) or len(set(runs)) != len(runs):
        found.append("AC2: the success run list is empty or not a strict run")
    if any(state != "success" for state in green.get("jobs", {}).values()):
        found.append("AC2: a job of the newest run is not success")
    clauses = record.get("ac2_clause_table", {})
    if clauses.get("removed_assertions") != []:
        found.append("AC2: the clause table lists a removed assertion")
    if "CUSTOMER_ONLY_PROVIDERS" not in clauses.get("kept_on_origin_dev", []):
        found.append("AC2: a kept clause is missing")
    for retired in clauses.get("retired_facts_not_restored", []):
        if not retired.get("retired_by"):
            found.append("AC2: a retired fact names no ruling")
    status = record.get("ac3_status_row", {})
    if (
        status.get("verdict") != "green"
        or "crit=C12 probe=c12-provider-catalogue verdict=green read_at="
        not in status.get("ledger_row", "")
    ):
        found.append("AC3: the STATUS row is not a green C12 row")
    return found


@pytest.fixture(scope="module")
def record() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(RECORD.read_text())
    return loaded


@pytest.mark.unit
def test_record_holds_all_three_criteria(record: dict[str, Any]) -> None:
    assert record["ticket_id"] == "OMN-26118"
    assert record["probe"] == "c12-provider-catalogue"
    assert refusals(record) == []


@pytest.mark.unit
def test_newest_run_is_the_last_of_six_successes(record: dict[str, Any]) -> None:
    green = record["ac2_green_runs"]
    assert green["run_numbers_success"] == [1314, 1315, 1316, 1317, 1318, 1319]
    assert green["newest_run_number"] == 1319


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (("ac1_cause", "newest_failure_text"), [], "AC1"),
        (("ac1_cause", "newest_failure_text", 0, "run_id"), "none", "AC1"),
        (("ac2_green_runs", "conclusion"), "failure", "AC2"),
        (("ac2_green_runs", "jobs", "139554008075"), "failure", "AC2"),
        (("ac2_clause_table", "removed_assertions"), ["no_house_rung"], "AC2"),
        (("ac2_clause_table", "kept_on_origin_dev"), [], "AC2"),
        (("ac3_status_row", "verdict"), "red", "AC3"),
    ],
)
def test_known_bad_records_are_refused(
    record: dict[str, Any], path: tuple[str | int, ...], value: Any, expected: str
) -> None:
    bad = copy.deepcopy(record)
    node: Any = bad
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert any(line.startswith(expected) for line in refusals(bad))
