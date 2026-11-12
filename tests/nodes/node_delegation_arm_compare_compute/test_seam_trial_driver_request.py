# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam with the trial driver's close route (lane eval-e1-driver-4720, plan E1): the paired request's wire shape.

The driver's close route sends one ModelArmCompareRequest per band and budget, built from its closed event
(per sequence: task key, model, band, budget label, last verdict). This test pins the request's field names and
required set, and builds a request from a closed-event-shaped fixture, so it fails when either side renames
or retypes a field. The driver's branch was not pushed when this was written; its side is the fixture.
"""

from __future__ import annotations

import pytest

from omnimarket.models.delegation_arm_compare.model_arm_compare_request import (
    ModelArmCompareRequest,
)
from omnimarket.models.delegation_arm_compare.model_arm_task_outcome import (
    ModelArmTaskOutcome,
)
from omnimarket.nodes.node_delegation_arm_compare_compute.handlers.handler_delegation_arm_compare import (
    HandlerDelegationArmCompare,
)
from tests.nodes.node_delegation_arm_compare_compute.support import overlay

pytestmark = pytest.mark.unit

REQUEST_FIELDS = {
    "comparison_id",
    "eval_run_id",
    "size_band",
    "budget_label",
    "arm_a_model",
    "arm_b_model",
    "outcomes_a",
    "outcomes_b",
    "control_outcomes_a",
    "control_outcomes_b",
}
REQUIRED = REQUEST_FIELDS - {"control_outcomes_a", "control_outcomes_b"}


def _closed_event() -> list[dict[str, str]]:
    rows = []
    for i in range(90):
        key = f"task-{i:03d}"
        for model, passes in (("model-a", 60), ("model-b", 75)):
            rows.append(
                {
                    "task_key": key,
                    "model": model,
                    "band": "S",
                    "budget": "b3",
                    "last": "pass" if i < passes else "fail",
                }
            )
    return rows


def _request_from(rows: list[dict[str, str]]) -> dict[str, object]:
    def arm(model: str) -> list[dict[str, str]]:
        return [
            {"task_key": r["task_key"], "outcome": r["last"]}
            for r in rows
            if r["model"] == model
        ]

    return {
        "comparison_id": "run-1:S:b3",
        "eval_run_id": "run-1",
        "size_band": "S",
        "budget_label": "b3",
        "arm_a_model": "model-a",
        "arm_b_model": "model-b",
        "outcomes_a": arm("model-a"),
        "outcomes_b": arm("model-b"),
    }


def test_request_wire_shape_is_pinned() -> None:
    assert set(ModelArmCompareRequest.model_fields) == REQUEST_FIELDS
    required = {
        name
        for name, f in ModelArmCompareRequest.model_fields.items()
        if f.is_required()
    }
    assert required == REQUIRED
    assert set(ModelArmTaskOutcome.model_fields) == {"task_key", "outcome"}


def test_a_request_built_from_a_closed_event_shaped_fixture_is_answered() -> None:
    request = ModelArmCompareRequest.model_validate(_request_from(_closed_event()))
    result = HandlerDelegationArmCompare(overlay=overlay()).handle(request).result
    assert result is not None
    assert (result.a_only, result.b_only, result.observed_n) == (0, 15, 90)
