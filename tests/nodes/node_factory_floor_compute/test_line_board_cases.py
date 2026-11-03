# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The six surprises of 2026-11-03 and an all-sources-unread case, each with a repaired variant (OMN-22367).

Lean line board plan T2 (OMN-22366 to OMN-22374 are its tasks). Each case is a frozen, time-stamped copy of the
sources the board reads (``fixtures/line_board_2026_11_03/<case>/``). The test runs the factory floor effect on it and
asserts the stop kinds and evidence ids the plan's section 2 requires; the repaired variant must carry none of them.

The compute has no ``stops`` field until T5 (OMN-22370) builds it, so every test is a strict xfail naming that ticket.
T5 removes the marks; a test that starts passing earlier fails the run (strict), so nothing passes by accident.
The effect also refuses an unread source today (T3, OMN-22368, turns the refusal into a health entry), which is why
``SourceRefusedError`` is an accepted failure.

Evidence ids are written as the ledger CITE-AS form (``KIND ts lane=<lane>``) and ``pr-context <pr> <context>``; T5 may
change the representation together with these fixtures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_factory_floor_effect.source_loader import SourceRefusedError

from .line_board_support import (
    CASES,
    VARIANTS,
    evidence_refs,
    header,
    run_board,
    stops_by_line,
)

T5_TICKET = "OMN-22370"
EXPECTED_TESTS = 14

_XFAIL = pytest.mark.xfail(
    strict=True,
    reason=f"{T5_TICKET}: the compute has no stops field until T5 builds the stop kinds",
    raises=(AssertionError, SourceRefusedError),
)

CASE_PARAMS = [
    pytest.param(case, variant, id=f"{case}-{variant}", marks=_XFAIL)
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


@pytest.mark.unit
@pytest.mark.parametrize(("case", "variant"), CASE_PARAMS)
def test_line_board_case(
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
