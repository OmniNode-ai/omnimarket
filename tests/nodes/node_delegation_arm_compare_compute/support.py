# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the arm comparison tests."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.models.delegation_arm_compare.enum_arm_compare_failure import (
    EnumArmCompareFailure,
)
from omnimarket.models.delegation_arm_compare.model_arm_compare_overlay import (
    ModelArmCompareOverlay,
)
from omnimarket.models.delegation_arm_compare.model_arm_compare_request import (
    ModelArmCompareRequest,
)
from omnimarket.models.delegation_arm_compare.model_arm_compare_result import (
    ModelArmCompareResult,
)
from omnimarket.models.delegation_arm_compare.model_arm_task_outcome import (
    ModelArmTaskOutcome,
)
from omnimarket.models.ranges.enum_range_sample_outcome import EnumRangeSampleOutcome

_FIXTURES = Path(__file__).parent / "fixtures" / "overlays"


def overlay() -> ModelArmCompareOverlay:
    """Load the test overlay fixture as a ModelArmCompareOverlay."""
    path = _FIXTURES / "node_delegation_arm_compare_compute" / "overlay.yaml"
    with path.open() as fh:
        return ModelArmCompareOverlay.model_validate(yaml.safe_load(fh))


def tasks(
    pairs: list[tuple[EnumRangeSampleOutcome, EnumRangeSampleOutcome]],
    *,
    prefix: str = "t",
) -> tuple[tuple[ModelArmTaskOutcome, ...], tuple[ModelArmTaskOutcome, ...]]:
    """Build the two arms' outcome tuples from a list of (a, b) outcome pairs."""
    outcomes_a = tuple(
        ModelArmTaskOutcome(task_key=f"{prefix}-{i:03d}", outcome=a)
        for i, (a, b) in enumerate(pairs)
    )
    outcomes_b = tuple(
        ModelArmTaskOutcome(task_key=f"{prefix}-{i:03d}", outcome=b)
        for i, (a, b) in enumerate(pairs)
    )
    return outcomes_a, outcomes_b


def request(
    outcomes_a: tuple[ModelArmTaskOutcome, ...],
    outcomes_b: tuple[ModelArmTaskOutcome, ...],
    *,
    comparison_id: str = "cmp-1",
    control_outcomes_a: tuple[ModelArmTaskOutcome, ...] = (),
    control_outcomes_b: tuple[ModelArmTaskOutcome, ...] = (),
) -> ModelArmCompareRequest:
    """Build a ModelArmCompareRequest from the two arms' outcome tuples."""
    return ModelArmCompareRequest(
        comparison_id=comparison_id,
        eval_run_id="eval-2026-10-arm",
        size_band="medium",
        budget_label="budget-1",
        arm_a_model="model-a",
        arm_b_model="model-b",
        outcomes_a=outcomes_a,
        outcomes_b=outcomes_b,
        control_outcomes_a=control_outcomes_a,
        control_outcomes_b=control_outcomes_b,
    )


def failure_result(
    comparison_id: str, failure: EnumArmCompareFailure, detail: str = ""
) -> ModelArmCompareResult:
    """Build a ModelArmCompareResult that is a typed failure (no verdict)."""
    return ModelArmCompareResult(
        comparison_id=comparison_id, failure=failure, detail=detail
    )


PASS = EnumRangeSampleOutcome.PASS
FAIL = EnumRangeSampleOutcome.FAIL


def planted(
    *, both_pass: int, both_fail: int, a_only: int, b_only: int, prefix: str = "t"
) -> tuple[tuple[ModelArmTaskOutcome, ...], tuple[ModelArmTaskOutcome, ...]]:
    """Plant discordant and concordant pairs: a_only means A passed and B failed."""
    pairs = (
        [(PASS, PASS)] * both_pass
        + [(FAIL, FAIL)] * both_fail
        + [(PASS, FAIL)] * a_only
        + [(FAIL, PASS)] * b_only
    )
    return tasks(pairs, prefix=prefix)
