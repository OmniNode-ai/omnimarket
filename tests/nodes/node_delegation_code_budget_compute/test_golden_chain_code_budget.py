# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of node_delegation_code_budget_compute (OMN-23434), each with named mutations that turn the chain red.

Every mutation in NAMED_MUTATIONS replaces one rule of the handler and runs the chain it breaks; the mutated chain must raise
AssertionError, which shows the assertion can fail. The unmutated chains are green in the controls.
"""

from __future__ import annotations

import importlib.resources
import math
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_delegation_code_budget import (
    EnumCodeBudgetRefusal,
    ModelCodeBudgetOverlay,
    ModelCodeBudgetRequest,
    ModelCodeBudgetResult,
    ModelDraftMeasurement,
)

pytestmark = pytest.mark.unit

NAME = "node_delegation_code_budget_compute"
OVERLAY_FILE = (
    Path(__file__).resolve().parent / "fixtures" / "overlays" / NAME / "overlay.yaml"
)


def contract() -> dict[str, Any]:
    """Load the contract YAML for the node."""
    files = importlib.resources.files(f"omnimarket.nodes.{NAME}")
    path = files.joinpath("contract.yaml")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_handler() -> Any:
    """The handler class the packaged contract routes its one operation to."""
    [entry] = contract()["handler_routing"]["handlers"]
    assert entry["operation"] == "delegation_code_budget"
    module = importlib.import_module(entry["handler"]["module"])
    return getattr(module, entry["handler"]["name"])


def load_overlay() -> ModelCodeBudgetOverlay:
    """Load and validate the fixture overlay."""
    with OVERLAY_FILE.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return ModelCodeBudgetOverlay.model_validate(data)


def build(
    operation: str,
    task_class: str,
    measurements: tuple[ModelDraftMeasurement, ...] = (),
) -> ModelCodeBudgetRequest:
    """Build a request with the fixture overlay."""
    return ModelCodeBudgetRequest(
        operation=operation,
        task_class=task_class,
        measurements=measurements,
        overlay=load_overlay(),
    )


def run_golden_chain() -> None:
    """Run the golden chain: resolve and size operations."""
    c = contract()
    assert c["node_type"] == "compute"
    assert c["descriptor"]["side_effects"] == []
    assert c["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.delegation-code-budget-resolved.v1"
    ]
    handler = resolve_handler()()

    # Resolve code_generation
    req = build("resolve", "code_generation")
    res = handler.handle(req)
    assert isinstance(res, ModelCodeBudgetResult)
    assert res.call_budget_s == 480.0
    assert res.source == "overlay"
    assert res.refusal is None

    # Size with four completed measurements
    m1 = ModelDraftMeasurement(engine="glm", duration_s=59.0, completed=True)
    m2 = ModelDraftMeasurement(engine="glm", duration_s=113.5, completed=True)
    m3 = ModelDraftMeasurement(engine="glm", duration_s=133.8, completed=True)
    m4 = ModelDraftMeasurement(engine="glm", duration_s=156.9, completed=True)
    req = build("size", "code_generation", (m1, m2, m3, m4))
    res = handler.handle(req)
    assert res.call_budget_s == 240.0
    assert res.source == "measured"
    assert res.completed_samples == 4
    assert res.refusal is None

    # A draft that timed out at 300.0 s is no sample: the slowest completed draft is 170.0
    m5 = ModelDraftMeasurement(engine="glm", duration_s=170.0, completed=True)
    m6 = ModelDraftMeasurement(engine="glm", duration_s=300.0, completed=False)
    m7 = ModelDraftMeasurement(engine="glm", duration_s=100.0, completed=True)
    m8 = ModelDraftMeasurement(engine="glm", duration_s=120.0, completed=True)
    req = build("size", "code_generation", (m5, m6, m7, m8))
    res = handler.handle(req)
    assert res.call_budget_s == 300.0
    assert res.completed_samples == 3
    assert res.refusal is None


def run_error_chain() -> None:
    """Run the error chain: unknown class, too few samples, over ceiling, invalid overlay."""
    handler = resolve_handler()()

    # Resolve unknown class
    req = build("resolve", "no_such_class")
    res = handler.handle(req)
    assert res.call_budget_s is None
    assert res.refusal is EnumCodeBudgetRefusal.UNKNOWN_TASK_CLASS

    # Size with only two completed measurements
    m1 = ModelDraftMeasurement(engine="glm", duration_s=50.0, completed=True)
    m2 = ModelDraftMeasurement(engine="glm", duration_s=60.0, completed=True)
    req = build("size", "code_generation", (m1, m2))
    res = handler.handle(req)
    assert res.refusal is EnumCodeBudgetRefusal.TOO_FEW_COMPLETED_SAMPLES
    assert res.completed_samples == 2
    assert res.call_budget_s is None

    # Size with three completed measurements of 500.0 s each -> raw 750 rounds up to 780, over the ceiling 600
    m3 = ModelDraftMeasurement(engine="glm", duration_s=500.0, completed=True)
    m4 = ModelDraftMeasurement(engine="glm", duration_s=500.0, completed=True)
    m5 = ModelDraftMeasurement(engine="glm", duration_s=500.0, completed=True)
    req = build("size", "code_generation", (m3, m4, m5))
    res = handler.handle(req)
    assert res.refusal is EnumCodeBudgetRefusal.OVER_CEILING
    assert res.call_budget_s is None
    assert "780.0" in res.detail
    assert "600.0" in res.detail

    # Overlay refuses construction with empty class_budgets_s
    with pytest.raises((ValidationError, ValueError)):
        ModelCodeBudgetOverlay(
            class_budgets_s={},
            sizing_margin=1.5,
            round_up_to_s=60,
            min_completed_samples=3,
            ceiling_s=600,
        )

    # Overlay refuses construction with a zero value
    with pytest.raises((ValidationError, ValueError)):
        ModelCodeBudgetOverlay(
            class_budgets_s={"code_generation": 0},
            sizing_margin=1.5,
            round_up_to_s=60,
            min_completed_samples=3,
            ceiling_s=600,
        )

    # Overlay refuses construction with sizing_margin 0.9
    with pytest.raises((ValidationError, ValueError)):
        ModelCodeBudgetOverlay(
            class_budgets_s={"code_generation": 480},
            sizing_margin=0.9,
            round_up_to_s=60,
            min_completed_samples=3,
            ceiling_s=600,
        )


def test_golden_chain() -> None:
    """Positive control: unmutated chain is green."""
    run_golden_chain()


def test_error_chain() -> None:
    """Positive control: unmutated error chain is green."""
    run_error_chain()


def test_mutation_timed_out_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: timed-out measurements are counted as completed."""
    module = importlib.import_module(
        "omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget"
    )
    original_handle = module.HandlerDelegationCodeBudget.handle

    def patched_handle(self, request: ModelCodeBudgetRequest) -> ModelCodeBudgetResult:
        # Rewrite every measurement to completed=True
        new_measurements = tuple(
            m.model_copy(update={"completed": True}) for m in request.measurements
        )
        mutated_request = request.model_copy(update={"measurements": new_measurements})
        return original_handle(self, mutated_request)

    monkeypatch.setattr(module.HandlerDelegationCodeBudget, "handle", patched_handle)
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_margin_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: sizing_margin is ignored (set to 1.0)."""
    module = importlib.import_module(
        "omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget"
    )
    original_handle = module.HandlerDelegationCodeBudget.handle

    def patched_handle(self, request: ModelCodeBudgetRequest) -> ModelCodeBudgetResult:
        # Set sizing_margin to 1.0
        mutated_overlay = request.overlay.model_copy(update={"sizing_margin": 1.0})
        mutated_request = request.model_copy(update={"overlay": mutated_overlay})
        return original_handle(self, mutated_request)

    monkeypatch.setattr(module.HandlerDelegationCodeBudget, "handle", patched_handle)
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_floor_instead_of_ceil(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: floor is used instead of ceil."""
    module = importlib.import_module(
        "omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget"
    )
    fake_math = types.SimpleNamespace(ceil=math.floor)
    monkeypatch.setattr(module, "math", fake_math)
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_unknown_class_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: unknown class is allowed and returns a budget."""
    module = importlib.import_module(
        "omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget"
    )
    original_handle = module.HandlerDelegationCodeBudget.handle

    def patched_handle(self, request: ModelCodeBudgetRequest) -> ModelCodeBudgetResult:
        # For resolve of an unknown class, return a result with call_budget_s 480.0 and refusal None
        if request.task_class not in request.overlay.class_budgets_s:
            return ModelCodeBudgetResult(
                operation=request.operation,
                task_class=request.task_class,
                call_budget_s=480.0,
                source="overlay",
                refusal=None,
                completed_samples=0,
                detail="",
            )
        return original_handle(self, request)

    monkeypatch.setattr(module.HandlerDelegationCodeBudget, "handle", patched_handle)
    with pytest.raises(AssertionError):
        run_error_chain()


NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "timed-out-counted": ("golden", "test_mutation_timed_out_counted"),
    "margin-ignored": ("golden", "test_mutation_margin_ignored"),
    "floor-instead-of-ceil": ("golden", "test_mutation_floor_instead_of_ceil"),
    "unknown-class-allowed": ("error", "test_mutation_unknown_class_allowed"),
}


def test_named_mutations_match_module() -> None:
    """Verify NAMED_MUTATIONS matches the test functions defined in the module."""
    defined_tests = {
        name
        for name, obj in vars(sys.modules[__name__]).items()
        if name.startswith("test_mutation_") and callable(obj)
    }
    expected_tests = {test_name for _, test_name in NAMED_MUTATIONS.values()}
    assert defined_tests == expected_tests
