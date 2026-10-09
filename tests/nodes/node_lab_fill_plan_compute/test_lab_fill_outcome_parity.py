# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Old-source expectations keep every remaining pure workflow decision honest (OMN-20668)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from .test_golden_chain_lab_fill_plan_compute import _contract, _resolve

FIXTURE = Path(__file__).parent / "fixtures" / "parity_cases_outcomes.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
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


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_outcomes_match_the_workflow(case: dict[str, Any]) -> None:
    handler_type, request_type, result_type = OPERATIONS[case["op"]]
    result = handler_type().handle(request_type.model_validate(case["input"]))
    assert isinstance(result, result_type)
    assert (
        result.model_dump(mode="json", by_alias=True, exclude_unset=True)
        == case["expected"]
    )
