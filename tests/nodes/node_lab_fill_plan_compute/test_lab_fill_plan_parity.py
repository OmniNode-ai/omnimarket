# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The planner decides what the lab-fill workflow decided, on the same inputs (OMN-20668).

The fixture holds each input and the answer the workflow's own pure block gave when
run under node, with the open fix to its candidate choice applied. Every case is run
through the packaged handler and the dumped result must equal that answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
    HandlerLabFillCandidateChoice,
    HandlerLabFillCapacity,
    HandlerLabFillDispatchPlan,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillCandidateChoiceRequest,
    ModelLabFillCapacityRequest,
    ModelLabFillDispatchPlanRequest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "parity_cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
OPERATIONS = {
    "plan_lab_fill_capacity": (HandlerLabFillCapacity, ModelLabFillCapacityRequest),
    "choose_lab_fill_candidates": (
        HandlerLabFillCandidateChoice,
        ModelLabFillCandidateChoiceRequest,
    ),
    "plan_lab_fill_dispatch": (
        HandlerLabFillDispatchPlan,
        ModelLabFillDispatchPlanRequest,
    ),
}


def test_fixture_covers_every_operation_with_enough_cases() -> None:
    by_op = {op: [c for c in CASES if c["op"] == op] for op in OPERATIONS}
    assert {op: len(cases) > 15 for op, cases in by_op.items()} == dict.fromkeys(
        OPERATIONS, True
    )
    assert len({c["name"] for c in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_planner_matches_the_workflow(case: dict[str, Any]) -> None:
    handler_type, request_type = OPERATIONS[str(case["op"])]
    request = request_type.model_validate(case["input"])
    result = handler_type().handle(request)
    assert (
        result.model_dump(mode="json", by_alias=True, exclude_unset=True)
        == case["expected"]
    )
