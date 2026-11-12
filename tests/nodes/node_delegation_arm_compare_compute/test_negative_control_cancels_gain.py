# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 of OMN-20017: an arm's gain that the negative control reproduces is not counted as a gain."""

from __future__ import annotations

import pytest

from omnimarket.models.ranges.enum_comparison_verdict import EnumComparisonVerdict
from omnimarket.nodes.node_delegation_arm_compare_compute.handlers.handler_delegation_arm_compare import (
    HandlerDelegationArmCompare,
)
from tests.nodes.node_delegation_arm_compare_compute.support import (
    overlay,
    planted,
    request,
)

pytestmark = pytest.mark.unit


def _gain() -> tuple:
    return planted(both_pass=70, both_fail=6, a_only=2, b_only=12)


def _control(**counts: int) -> tuple:
    return planted(prefix="c", **counts)


def _handle(control: tuple | None):
    a, b = _gain()
    ca, cb = control if control else ((), ())
    return HandlerDelegationArmCompare(overlay=overlay()).handle(
        request(a, b, control_outcomes_a=ca, control_outcomes_b=cb)
    )


def test_negative_control_cancels_gain_when_the_control_reproduces_it() -> None:
    answer = _handle(_control(both_pass=70, both_fail=6, a_only=2, b_only=12))
    assert answer.result is not None
    assert answer.control is not None
    assert answer.control.verdict is EnumComparisonVerdict.DIFFERENCE
    assert answer.result.verdict is EnumComparisonVerdict.NO_DIFFERENCE
    assert answer.control_cancelled_gain is True
    assert any("negative control" in reason for reason in answer.result.reasons)
    assert answer.result.b_only == 12
    assert answer.result.a_only == 2


def test_negative_control_cancels_gain_not_when_the_control_is_quiet() -> None:
    answer = _handle(_control(both_pass=70, both_fail=6, a_only=7, b_only=7))
    assert answer.result is not None
    assert answer.control is not None
    assert answer.control.verdict is EnumComparisonVerdict.NO_DIFFERENCE
    assert answer.result.verdict is EnumComparisonVerdict.DIFFERENCE
    assert answer.control_cancelled_gain is False


def test_negative_control_cancels_gain_not_when_there_is_no_control() -> None:
    answer = _handle(None)
    assert answer.result is not None
    assert answer.result.verdict is EnumComparisonVerdict.DIFFERENCE
    assert answer.control is None
    assert answer.control_cancelled_gain is False


def test_negative_control_cancels_gain_not_when_the_control_runs_the_other_way() -> (
    None
):
    answer = _handle(_control(both_pass=70, both_fail=6, a_only=12, b_only=2))
    assert answer.control is not None
    assert answer.control.verdict is EnumComparisonVerdict.DIFFERENCE
    assert answer.result is not None
    assert answer.result.verdict is EnumComparisonVerdict.DIFFERENCE
    assert answer.control_cancelled_gain is False
