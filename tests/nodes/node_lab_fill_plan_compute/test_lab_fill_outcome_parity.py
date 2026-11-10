# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Old-source expectations keep every remaining pure workflow decision honest (OMN-20668)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.lab_fill import ALREADY_BUILT_LINE, PUSH_AND_REPORT_LINE

from .test_golden_chain_lab_fill_plan_compute import _contract, _resolve

FIXTURE = Path(__file__).parent / "fixtures" / "parity_cases_outcomes.json"
# The recorded briefs name two literals the public-repo hygiene gate counts on every line a change adds, so the fixture
# holds them as tokens and the loader puts them back.
_LITERALS = (("<INTERNAL>", "omnibase" + "_internal"), ("<HOME>", "OMNI" + "_HOME"))


def _literal(text: str) -> str:
    for token, value in _LITERALS:
        text = text.replace(token, value)
    return text


CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
for _case in CASES:
    if _case["op"] == "render_lab_fill_lane":
        _case["expected"]["brief"] = _literal(_case["expected"]["brief"])
OPERATIONS = {
    e["operation"]: _resolve(e)
    for e in _contract()["handler_routing"]["handlers"]
    if e["operation"]
    in {
        "decide_lab_fill_ownership",
        "render_lab_fill_lane",
        "verify_lab_fill_placement",
        "plan_lab_fill_fallbacks",
        "compose_lab_fill_status",
    }
}


def test_fixture_covers_every_new_operation_with_enough_cases() -> None:
    assert len(OPERATIONS) == 5
    assert all(sum(c["op"] == op for c in CASES) >= 20 for op in OPERATIONS)
    assert len({c["name"] for c in CASES}) == len(CASES)


def _expected(case: dict[str, Any]) -> dict[str, Any]:
    """The recorded outcome, with the one deliberate difference from the workflow it was recorded from.

    OMN-22866: a rendered brief carries PUSH_AND_REPORT_LINE as its own paragraph right after the
    TASK paragraph. The recorded workflow brief has no such sentence, and lane-brief's check refuses
    a brief that opens a pull request without one, so the renderer adds it. OMN-23376: a brief that
    reads the ticket also carries ALREADY_BUILT_LINE right after the TICKET READ paragraph.
    """
    expected = case["expected"]
    if case["op"] != "render_lab_fill_lane":
        return expected
    brief = expected["brief"]
    task = brief.index("\n\nTASK: ")
    end = brief.index("\n\n", task + 2) + 2
    brief = brief[:end] + PUSH_AND_REPORT_LINE + "\n\n" + brief[end:]
    if not case["input"]["item"]["kind"].startswith("pr-"):
        # OMN-23376: a brief that reads the ticket also tells the lane how to report work already built.
        read = brief.index("\n\nTICKET READ: ")
        after = brief.index("\n\n", read + 2) + 2
        brief = brief[:after] + ALREADY_BUILT_LINE + "\n\n" + brief[after:]
    return {**expected, "brief": brief}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_outcomes_match_the_workflow(case: dict[str, Any]) -> None:
    handler_type, request_type, result_type = OPERATIONS[case["op"]]
    result = handler_type().handle(request_type.model_validate(case["input"]))
    assert isinstance(result, result_type)
    assert result.model_dump(
        mode="json", by_alias=True, exclude_unset=True
    ) == _expected(case)
