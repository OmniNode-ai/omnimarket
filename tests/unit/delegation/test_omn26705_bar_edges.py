# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26705: the edges of the quality bar never read quality_bar_met below the bar.

Known good: a score at or above the bar reads quality_bar_met. Known bad: a score one
thousandth under the bar, with or without an UNDETERMINED rubric, never does.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceReason,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    derive_attempt_acceptance,
)

pytestmark = pytest.mark.unit


def _reason(score: float, bar: float) -> EnumDelegationAcceptanceReason:
    return derive_attempt_acceptance(
        quality_passed=True,
        pre_filter_rejected=False,
        gate_passed=True,
        quality_score=score,
        required_bar=bar,
        rule_evaluations=(),
    )[1]


@pytest.mark.parametrize("score", [0.849, 0.733, 0.0])
def test_ac1_known_bad_below_bar_never_reads_quality_bar_met(score: float) -> None:
    assert _reason(score, 0.850) is not EnumDelegationAcceptanceReason.QUALITY_BAR_MET


@pytest.mark.parametrize("score", [0.850, 0.851, 1.0])
def test_ac1_known_good_at_or_above_bar_reads_quality_bar_met(score: float) -> None:
    assert _reason(score, 0.850) is EnumDelegationAcceptanceReason.QUALITY_BAR_MET
