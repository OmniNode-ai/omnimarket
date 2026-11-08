# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The six surprises of 2026-11-03 and an all-sources-unread case, each with a repaired variant (OMN-22367).

Lean line board plan T2 (OMN-22366 to OMN-22374 are its tasks). Each case is a frozen, time-stamped copy of the
sources the board reads (``fixtures/line_board_2026_11_03/<case>/``). The test runs the factory floor effect on it and
asserts the stop kinds and evidence ids the plan's section 2 requires; the repaired variant must carry none of them.

The compute builds the stops (OMN-22370, T5), so each test asserts its case directly. The effect turns an unread source
into a health entry (T3, OMN-22368), so a case with every source absent runs and reads ``unread``.

Evidence ids are written as the ledger CITE-AS form (``KIND ts lane=<lane>``) and ``pr-context <pr> <context>``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .line_board_support import (
    CASES,
    VARIANTS,
    evidence_refs,
    header,
    run_board,
    stops_by_line,
)

EXPECTED_TESTS = 14

CASE_PARAMS = [
    pytest.param(case, variant, id=f"{case}-{variant}")
    for case in CASES
    for variant in VARIANTS
]

assert len(CASE_PARAMS) == EXPECTED_TESTS, (
    "T2-A: seven cases, each with a repaired variant"
)


def _check_present(expected: dict[str, Any], record: dict[str, Any], key: str) -> None:
    found = stops_by_line(record)
    for want in expected.get(key, []):
        stop = found.get((want["line"], want["kind"]))
        assert stop is not None, f"no {want['kind']} stop on line {want['line']}"
        missing = set(want.get("evidence", [])) - evidence_refs(stop)
        assert not missing, (
            f"{want['kind']} on {want['line']} lacks evidence {sorted(missing)}"
        )
        if "sentence_contains" in want:
            assert want["sentence_contains"] in stop.get("sentence", "")


def _check_absent(expected: dict[str, Any], record: dict[str, Any]) -> None:
    for gone in expected.get("absent", []):
        for (line, kind), stop in stops_by_line(record).items():
            if kind != gone["kind"] or gone.get("line", line) != line:
                continue
            if "pr" in gone and not any(
                gone["pr"] in ref for ref in evidence_refs(stop)
            ):
                continue
            named = set(gone.get("evidence", []))
            # A stop of this kind with none of the named evidence is another stop; with no evidence named, any is.
            assert named, f"{kind} is still on {line}"
            assert not (named & evidence_refs(stop)), f"{kind} is still on {line}"


def _check_health(expected: dict[str, Any], record: dict[str, Any]) -> None:
    health = record.get("sources", {}).get("health")
    assert health, "the record carries no source health"
    states = {entry["source"]: entry["state"] for entry in health}
    for source, state in expected["health"].items():
        assert states.get(source) == state, f"{source}: {states.get(source)} != {state}"
    if "coverage" in expected:
        assert expected["coverage"] in str(record.get("coverage", ""))


def _assert_case(
    case: str, variant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = header(case, variant)["expected"]
    record = run_board(case, variant, tmp_path, monkeypatch)
    lines = record.get("lines", [])
    assert lines, "the record has no line"
    assert all("stops" in line for line in lines), "no line carries a stops field"
    if "health" in expected:
        _check_health(expected, record)
    _check_present(expected, record, "stops")
    _check_present(expected, record, "still_present")
    _check_absent(expected, record)
    if variant == "observed":
        assert expected.get("stops") or expected.get("health"), (
            "an observed case names a stop"
        )


@pytest.mark.unit
@pytest.mark.parametrize(("case", "variant"), CASE_PARAMS)
def test_line_board_case(
    case: str, variant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_case(case, variant, tmp_path, monkeypatch)
