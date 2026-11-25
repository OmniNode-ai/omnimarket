# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The output_budget operation of node_delegation_code_budget_compute (OMN-26640).

A backend's output budget is its window less the request's measured input tokens less the
declared margin, held to the declared ceiling. Every number is the overlay's: the window, the margin,
the floor and the ceiling travel on the request, the input tokens are the caller's measurement, and the
handler holds no value of its own. A backend the overlay does not name is refused, and so is a request
whose room is under the declared floor.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_delegation_code_budget import (
    EnumCodeBudgetRefusal,
    ModelCodeBudgetOverlay,
    ModelCodeBudgetRequest,
    ModelOutputBudgetSetting,
)
from omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget import (
    HandlerDelegationCodeBudget,
)

pytestmark = pytest.mark.unit

BACKEND = "inkling-lab"


def overlay_data(**setting: int) -> dict[str, Any]:
    values = {
        "window": 65536,
        "margin": 512,
        "floor": 1024,
        "ceiling": 65536,
        "measure_timeout_s": 5,
    }
    values.update(setting)
    return {
        "class_budgets_s": {"code_generation": 600},
        "sizing_margin": 1.5,
        "round_up_to_s": 60,
        "min_completed_samples": 3,
        "ceiling_s": 600,
        "input_ceilings": {"local-coder": {"code_generation": 65536}},
        "output_budgets": {BACKEND: values},
    }


def request(input_tokens: int, backend: str = BACKEND, **setting: int):
    return ModelCodeBudgetRequest(
        operation="output_budget",
        task_class="code_generation",
        backend=backend,
        input_tokens=input_tokens,
        overlay=ModelCodeBudgetOverlay.model_validate(overlay_data(**setting)),
    )


def test_golden_budget_is_window_less_input_less_margin() -> None:
    result = HandlerDelegationCodeBudget().handle(request(1471))
    assert result.refusal is None
    assert result.output_budget is not None
    assert result.output_budget.tokens == 65536 - 1471 - 512
    assert result.output_budget.input_tokens == 1471
    assert result.output_budget.window == 65536
    assert result.output_budget.margin == 512


def test_each_request_gets_its_own_budget() -> None:
    handler = HandlerDelegationCodeBudget()
    small = handler.handle(request(833)).output_budget
    large = handler.handle(request(20000)).output_budget
    assert small is not None
    assert large is not None
    assert small.tokens == 65536 - 833 - 512
    assert large.tokens == 65536 - 20000 - 512
    assert small.tokens > large.tokens


def test_the_declared_ceiling_holds_a_large_room() -> None:
    result = HandlerDelegationCodeBudget().handle(request(100, ceiling=60000))
    assert result.output_budget is not None
    assert result.output_budget.tokens == 60000


def test_a_larger_window_in_the_overlay_gives_a_larger_budget() -> None:
    result = HandlerDelegationCodeBudget().handle(
        request(1000, window=131072, ceiling=131072)
    )
    assert result.output_budget is not None
    assert result.output_budget.tokens == 131072 - 1000 - 512


def test_room_under_the_floor_is_refused_with_no_tokens() -> None:
    result = HandlerDelegationCodeBudget().handle(request(65536 - 512 - 1023))
    assert result.refusal is EnumCodeBudgetRefusal.NO_OUTPUT_ROOM
    assert result.output_budget is None
    assert "1023" in result.detail


def test_room_exactly_at_the_floor_is_granted() -> None:
    result = HandlerDelegationCodeBudget().handle(request(65536 - 512 - 1024))
    assert result.refusal is None
    assert result.output_budget is not None
    assert result.output_budget.tokens == 1024


def test_a_backend_the_overlay_does_not_name_is_refused() -> None:
    result = HandlerDelegationCodeBudget().handle(request(10, backend="unnamed"))
    assert result.refusal is EnumCodeBudgetRefusal.UNNAMED_OUTPUT_BUDGET
    assert result.output_budget is None


def test_an_overlay_without_output_budgets_names_no_backend() -> None:
    data = overlay_data()
    del data["output_budgets"]
    result = HandlerDelegationCodeBudget().handle(
        ModelCodeBudgetRequest(
            operation="output_budget",
            task_class="code_generation",
            backend=BACKEND,
            input_tokens=10,
            overlay=ModelCodeBudgetOverlay.model_validate(data),
        )
    )
    assert result.refusal is EnumCodeBudgetRefusal.UNNAMED_OUTPUT_BUDGET


def test_the_request_names_a_backend_and_the_measured_input() -> None:
    overlay = ModelCodeBudgetOverlay.model_validate(overlay_data())
    with pytest.raises(ValidationError):
        ModelCodeBudgetRequest(
            operation="output_budget",
            task_class="code_generation",
            input_tokens=10,
            overlay=overlay,
        )
    with pytest.raises(ValidationError):
        ModelCodeBudgetRequest(
            operation="output_budget",
            task_class="code_generation",
            backend=BACKEND,
            overlay=overlay,
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"window": 0},
        {"margin": -1},
        {"floor": 0},
        {"ceiling": 0},
        {"measure_timeout_s": 0},
        {"floor": 5000, "ceiling": 4000},
        {"window": 1000, "margin": 600, "floor": 500},
    ],
)
def test_a_setting_that_cannot_hold_a_budget_is_refused_at_load(
    bad: dict[str, int],
) -> None:
    with pytest.raises(ValidationError):
        ModelOutputBudgetSetting.model_validate(
            {
                "window": 65536,
                "margin": 512,
                "floor": 1024,
                "ceiling": 60000,
                "measure_timeout_s": 5,
            }
            | bad
        )


def test_no_setting_has_a_default() -> None:
    for key in ("window", "margin", "floor", "ceiling", "measure_timeout_s"):
        data = {
            "window": 65536,
            "margin": 512,
            "floor": 1024,
            "ceiling": 60000,
            "measure_timeout_s": 5,
        }
        del data[key]
        with pytest.raises(ValidationError):
            ModelOutputBudgetSetting.model_validate(data)
