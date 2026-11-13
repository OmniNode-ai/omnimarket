# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J5 windows (OMN-23717): the poll, the run deadline and the publish step's limit are held to each other by test.

The poll's wait comes from the overlay (attempts and interval), the run's own limit from the overlay (``run_deadline_seconds``),
and the publish step's limit from the contract of ``node_factory_floor_publish_effect``. Three rules hold them together:
the poll's wait fits inside the run deadline (the overlay refuses to load otherwise), the run deadline sits below the step's
limit (so a run still open closes its chain typed before the step kills it), and the publish contract's longest declared path
stays under its cycle bound. Golden: the example overlay and the real contract satisfy all three. Error: an overlay whose
wait passes the deadline is refused naming both numbers; a step limit at or below the deadline, or a longest path past the
bound, is named. The measured append-to-visible delay (p95 105.1 s) is not covered by these windows: the pull request body
names the arithmetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishRuntime,
)
from omnimarket.nodes.node_work_ledger_append_effect.overlay_join_j5 import (
    ModelJoinJ5Overlay,
)
from tests.nodes.node_factory_floor_publish_effect.limits_support import TEST_LIMITS

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_ledger_append_effect/overlay_join_j5.example.yaml"
)
STEP = "join_j5"


def _document() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def _overlay(**projection: Any) -> ModelJoinJ5Overlay:
    document = _document()
    document["projection"].update(projection)
    return ModelJoinJ5Overlay.model_validate(document)


def _step_limit(runtime: ModelFactoryFloorPublishRuntime) -> int:
    return next(s.timeout_seconds for s in runtime.steps if s.name == STEP)


def window_problems(
    overlay: ModelJoinJ5Overlay, runtime: ModelFactoryFloorPublishRuntime
) -> list[str]:
    """Every way the J5 windows and the publish contract disagree, one line each."""
    problems: list[str] = []
    limit = _step_limit(runtime)
    if overlay.run_deadline_seconds >= limit:
        problems.append(
            f"run_deadline_seconds={overlay.run_deadline_seconds} is not below the {STEP} step limit of {limit} s"
        )
    path = sum(runtime.declared_longest_path().values())
    if path > runtime.cycle.bound_seconds:
        problems.append(
            f"the longest declared path is {path} s, past the cycle bound of {runtime.cycle.bound_seconds} s"
        )
    return problems


def test_golden_the_example_overlay_and_the_real_contract_agree() -> None:
    overlay = _overlay()

    assert overlay.projection_wait_seconds == 25
    assert window_problems(overlay, load_runtime().with_limits(TEST_LIMITS)) == []


def test_error_a_poll_that_waits_past_the_run_deadline_is_refused_with_both_numbers() -> (
    None
):
    document = _document()
    deadline = document["run_deadline_seconds"]
    document["projection"]["attempts"] = 40
    document["projection"]["interval_seconds"] = 5

    with pytest.raises(ValidationError) as refused:
        ModelJoinJ5Overlay.model_validate(document)

    text = str(refused.value)
    assert "195" in text
    assert f"run_deadline_seconds={deadline}" in text


def test_control_a_wait_one_second_inside_the_deadline_still_loads() -> None:
    deadline = _document()["run_deadline_seconds"]
    overlay = _overlay(attempts=2, interval_seconds=deadline - 1)

    assert overlay.projection_wait_seconds == deadline - 1


def test_error_a_run_deadline_at_the_step_limit_is_named() -> None:
    runtime = load_runtime().with_limits(TEST_LIMITS)
    document = _document()
    document["run_deadline_seconds"] = _step_limit(runtime)
    document["projection"]["attempts"] = 2
    overlay = ModelJoinJ5Overlay.model_validate(document)

    [found] = window_problems(overlay, runtime)

    assert "is not below the join_j5 step limit" in found


def test_error_a_longest_path_past_the_cycle_bound_is_named() -> None:
    runtime = load_runtime().with_limits(TEST_LIMITS)
    steps = tuple(
        s.model_copy(update={"timeout_seconds": runtime.cycle.bound_seconds})
        if s.name == STEP
        else s
        for s in runtime.steps
    )
    slow = runtime.model_copy(update={"steps": steps})

    found = window_problems(_overlay(), slow)

    assert any("past the cycle bound" in line for line in found)
