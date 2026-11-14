# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The input_ceiling operation of node_delegation_code_budget_compute (OMN-23631).

A named backend and task class resolves to the overlay's ceiling with its overlay
key recorded; a pair the overlay does not name is refused with UNNAMED_INPUT_CEILING
and no tokens. The overlay refuses to exist without a non-empty, all-positive
``input_ceilings``, resolve and size are unchanged, and no packaged config carries
the retired ``measured_correct_ceilings`` key.
"""

from __future__ import annotations

import importlib.resources
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
    input_ceiling_overlay_key,
)
from omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget import (
    HandlerDelegationCodeBudget,
)

pytestmark = pytest.mark.unit

NAME = "node_delegation_code_budget_compute"
OVERLAY_FILE = (
    Path(__file__).resolve().parent / "fixtures" / "overlays" / NAME / "overlay.yaml"
)


def fixture_overlay_data() -> dict[str, Any]:
    """The fixture overlay as unparsed data."""
    with OVERLAY_FILE.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_overlay() -> ModelCodeBudgetOverlay:
    """Load and validate the fixture overlay."""
    return ModelCodeBudgetOverlay.model_validate(fixture_overlay_data())


def build(
    operation: str,
    task_class: str,
    backend: str | None = None,
    measurements: tuple[ModelDraftMeasurement, ...] = (),
) -> ModelCodeBudgetRequest:
    """Build a request with the fixture overlay."""
    return ModelCodeBudgetRequest(
        operation=operation,
        task_class=task_class,
        backend=backend,
        measurements=measurements,
        overlay=load_overlay(),
    )


@pytest.mark.parametrize(
    ("backend", "task_class", "expected_tokens", "expected_key"),
    [
        (
            "local-coder",
            "code_generation",
            8169,
            "delegation.code_budget.input_ceilings.local-coder.code_generation",
        ),
        (
            "fixture-backend",
            "code_generation",
            1000,
            "delegation.code_budget.input_ceilings.fixture-backend.code_generation",
        ),
        (
            "fixture-backend",
            "test",
            2000,
            "delegation.code_budget.input_ceilings.fixture-backend.test",
        ),
    ],
)
def test_input_ceiling_named_pair(
    backend: str, task_class: str, expected_tokens: int, expected_key: str
) -> None:
    """A named pair returns the overlay's ceiling, from the overlay, with its key."""
    result = HandlerDelegationCodeBudget().handle(
        build("input_ceiling", task_class, backend=backend)
    )
    assert isinstance(result, ModelCodeBudgetResult)
    assert result.input_ceiling_tokens == expected_tokens
    assert result.source == "overlay"
    assert result.refusal is None
    assert result.call_budget_s is None
    assert result.completed_samples == 0
    assert result.backend == backend
    assert result.task_class == task_class
    assert result.overlay_key == expected_key
    assert result.overlay_key == input_ceiling_overlay_key(backend, task_class)


@pytest.mark.parametrize(
    ("backend", "task_class"),
    [("local-coder", "unlisted_class"), ("unknown-backend", "code_generation")],
)
def test_input_ceiling_refuses_unnamed_pair(backend: str, task_class: str) -> None:
    """A pair the overlay does not name is refused, naming the overlay key."""
    result = HandlerDelegationCodeBudget().handle(
        build("input_ceiling", task_class, backend=backend)
    )
    assert result.refusal is EnumCodeBudgetRefusal.UNNAMED_INPUT_CEILING
    assert result.input_ceiling_tokens is None
    assert result.call_budget_s is None
    assert result.source == "overlay"
    assert result.overlay_key == input_ceiling_overlay_key(backend, task_class)
    assert input_ceiling_overlay_key(backend, task_class) in result.detail


def test_input_ceiling_requires_backend() -> None:
    """A request for an input ceiling without a backend is not a request."""
    with pytest.raises(ValidationError):
        build("input_ceiling", "code_generation")


def test_overlay_refuses_empty_input_ceilings() -> None:
    """The overlay refuses to exist with an empty input_ceilings."""
    with pytest.raises(ValidationError):
        ModelCodeBudgetOverlay.model_validate(
            {**fixture_overlay_data(), "input_ceilings": {}}
        )


def test_overlay_refuses_zero_input_ceiling() -> None:
    """The overlay refuses a ceiling that is not a positive number of tokens."""
    with pytest.raises(ValidationError):
        ModelCodeBudgetOverlay.model_validate(
            {
                **fixture_overlay_data(),
                "input_ceilings": {"local-coder": {"code_generation": 0}},
            }
        )


def test_overlay_refuses_missing_input_ceilings() -> None:
    """The overlay refuses to exist without the input_ceilings key at all."""
    data = {
        key: value
        for key, value in fixture_overlay_data().items()
        if key != "input_ceilings"
    }
    with pytest.raises(ValidationError):
        ModelCodeBudgetOverlay.model_validate(data)


def test_no_packaged_measured_correct_ceilings_key() -> None:
    """No packaged config carries the retired measured_correct_ceilings key.

    Since OMN-23631 the ceilings live in node_delegation_code_budget_compute's
    overlay; a config that still named them would be a second, unversioned
    source of truth for the same value.
    """
    configs = importlib.resources.files("omnimarket").joinpath("configs")
    with importlib.resources.as_file(configs) as path:
        packaged = [
            (file_path, file_path.read_bytes())
            for file_path in sorted(path.rglob("*"))
            if file_path.is_file()
        ]
    assert packaged, "the packaged configs directory holds no files"
    for file_path, raw in packaged:
        assert b"measured_correct_ceilings" not in raw, str(file_path)


def test_resolve_and_size_still_work_against_fixture_overlay() -> None:
    """The two operations the input ceiling joined are unchanged."""
    handler = HandlerDelegationCodeBudget()

    resolved = handler.handle(build("resolve", "code_generation"))
    assert resolved.call_budget_s == 480.0
    assert resolved.source == "overlay"
    assert resolved.refusal is None

    measurements = (
        ModelDraftMeasurement(engine="glm", duration_s=100.0, completed=True),
        ModelDraftMeasurement(engine="glm", duration_s=200.0, completed=True),
        ModelDraftMeasurement(engine="glm", duration_s=150.0, completed=True),
    )
    sized = handler.handle(build("size", "code_generation", measurements=measurements))
    assert sized.call_budget_s == 300.0
    assert sized.source == "measured"
    assert sized.completed_samples == 3
    assert sized.refusal is None

    unknown = handler.handle(build("resolve", "no_such_class"))
    assert unknown.call_budget_s is None
    assert unknown.refusal is EnumCodeBudgetRefusal.UNKNOWN_TASK_CLASS
