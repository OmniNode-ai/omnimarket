# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 of OMN-20017: the compute pairs by task and reports wins, losses and the exact p-value."""

from __future__ import annotations

import math

import pytest

from omnimarket.models.ranges.enum_comparison_verdict import EnumComparisonVerdict
from omnimarket.nodes.node_delegation_arm_compare_compute.handlers.handler_delegation_arm_compare import (
    HandlerDelegationArmCompare,
)
from tests.nodes.node_delegation_arm_compare_compute.support import (
    FAIL,
    PASS,
    overlay,
    planted,
    request,
    tasks,
)

pytestmark = pytest.mark.unit


def _handler() -> HandlerDelegationArmCompare:
    return HandlerDelegationArmCompare(overlay=overlay())


def _exact_two_sided(a_only: int, b_only: int) -> float:
    m = a_only + b_only
    smaller = min(a_only, b_only)
    return min(1.0, 2 * sum(math.comb(m, k) for k in range(smaller + 1)) / 2**m)


def test_exact_paired_planted_difference_reports_wins_losses_and_exact_p() -> None:
    a, b = planted(both_pass=70, both_fail=6, a_only=2, b_only=12)
    answer = _handler().handle(request(a, b))
    assert answer.failure is None
    assert answer.result is not None
    result = answer.result
    assert result.verdict is EnumComparisonVerdict.DIFFERENCE
    assert (result.a_only, result.b_only, result.observed_n) == (2, 12, 90)
    assert result.p_value == pytest.approx(_exact_two_sided(2, 12))
    assert result.p_value == pytest.approx(212 / 16384)
    assert result.difference == pytest.approx(10 / 90)


def test_exact_paired_planted_equal_split_is_no_difference() -> None:
    a, b = planted(both_pass=70, both_fail=6, a_only=7, b_only=7)
    result = _handler().handle(request(a, b)).result
    assert result is not None
    assert result.verdict is EnumComparisonVerdict.NO_DIFFERENCE
    assert result.p_value == pytest.approx(1.0)


def test_exact_paired_planted_below_required_n_is_refused_with_the_required_n() -> None:
    a, b = planted(both_pass=10, both_fail=0, a_only=0, b_only=10)
    result = _handler().handle(request(a, b)).result
    assert result is not None
    assert result.verdict is EnumComparisonVerdict.REFUSED
    assert result.required_n == 90
    assert any("required n=90" in reason for reason in result.reasons)


def test_exact_paired_planted_pairs_by_task_key_not_by_position() -> None:
    a, b = planted(both_pass=70, both_fail=6, a_only=2, b_only=12)
    in_order = _handler().handle(request(a, b)).result
    shuffled = _handler().handle(request(a, tuple(reversed(b)))).result
    assert in_order is not None
    assert shuffled is not None
    assert (shuffled.a_only, shuffled.b_only) == (in_order.a_only, in_order.b_only)
    assert shuffled.p_value == in_order.p_value


def test_exact_paired_planted_a_task_one_arm_never_answered_counts_as_a_failure() -> (
    None
):
    a, b = tasks([(PASS, PASS)] * 89 + [(PASS, FAIL)])
    answer = _handler().handle(request(a, b[:-1]))
    result = answer.result
    assert result is not None
    assert result.observed_n == 90
    assert result.incomplete_b == 1
    assert result.a_only == 1
