# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 of OMN-20017: failing controls. Each injected break turns its reading red; the unbroken handler reads green.

Break 1 "compare unpaired rows": the arms are paired by position, never by task key.
Break 2 "count the control's gain": the negative control is dropped, so a reproduced gain is counted.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from omnimarket.models.delegation_arm_compare.model_arm_compare_request import (
    ModelArmCompareRequest,
)
from omnimarket.models.delegation_arm_compare.model_arm_compare_result import (
    ModelArmCompareResult,
)
from omnimarket.models.delegation_arm_compare.model_arm_task_outcome import (
    ModelArmTaskOutcome,
)
from omnimarket.models.ranges.enum_comparison_verdict import EnumComparisonVerdict
from omnimarket.models.ranges.model_comparison_pair import ModelComparisonPair
from omnimarket.nodes.node_delegation_arm_compare_compute.handlers.handler_delegation_arm_compare import (
    HandlerDelegationArmCompare,
)
from tests.nodes.node_delegation_arm_compare_compute.support import (
    overlay,
    planted,
    request,
)

pytestmark = pytest.mark.unit


class CompareUnpairedRows(HandlerDelegationArmCompare):
    """Break 1: zip the two arms by position."""

    def _pair(
        self,
        outcomes_a: Sequence[ModelArmTaskOutcome],
        outcomes_b: Sequence[ModelArmTaskOutcome],
    ) -> tuple[ModelComparisonPair, ...]:
        return tuple(
            ModelComparisonPair(
                case_id=a.task_key, outcome_a=a.outcome, outcome_b=b.outcome
            )
            for a, b in zip(outcomes_a, outcomes_b, strict=True)
        )


class CountTheControlsGain(HandlerDelegationArmCompare):
    """Break 2: the negative control never reaches the comparison."""

    def handle(self, request: ModelArmCompareRequest) -> ModelArmCompareResult:
        return super().handle(
            request.model_copy(
                update={"control_outcomes_a": (), "control_outcomes_b": ()}
            )
        )


def check_pairing_is_by_task_key(handler: HandlerDelegationArmCompare) -> None:
    a, b = planted(both_pass=70, both_fail=6, a_only=2, b_only=12)
    answer = handler.handle(request(a, tuple(reversed(b))))
    assert answer.result is not None
    assert (answer.result.a_only, answer.result.b_only) == (2, 12)


def check_reproduced_gain_is_not_counted(handler: HandlerDelegationArmCompare) -> None:
    a, b = planted(both_pass=70, both_fail=6, a_only=2, b_only=12)
    ca, cb = planted(both_pass=70, both_fail=6, a_only=2, b_only=12, prefix="c")
    answer = handler.handle(request(a, b, control_outcomes_a=ca, control_outcomes_b=cb))
    assert answer.result is not None
    assert answer.result.verdict is EnumComparisonVerdict.NO_DIFFERENCE


def test_the_unbroken_handler_reads_green() -> None:
    check_pairing_is_by_task_key(HandlerDelegationArmCompare(overlay=overlay()))
    check_reproduced_gain_is_not_counted(HandlerDelegationArmCompare(overlay=overlay()))


def test_break_compare_unpaired_rows_turns_the_pairing_check_red() -> None:
    with pytest.raises(AssertionError):
        check_pairing_is_by_task_key(CompareUnpairedRows(overlay=overlay()))


def test_break_count_the_controls_gain_turns_the_control_check_red() -> None:
    with pytest.raises(AssertionError):
        check_reproduced_gain_is_not_counted(CountTheControlsGain(overlay=overlay()))
