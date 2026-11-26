# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26705: an attempt scored below its bar is never accepted as quality_bar_met.

Measured 2026-11-26 on published 0.4.328 (run 01ee4a7c-88e0-4cfc-b556-9d086a3f5219):
acceptance_detail "actual_score=0.733 required_bar=0.850 score_vs_bar=below_bar"
beside acceptance=accept reason=quality_bar_met and rubric UNDETERMINED.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
    ModelAttemptRubricVerdict,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    derive_attempt_acceptance,
)

pytestmark = pytest.mark.unit

_UNDETERMINED = ModelAttemptRubricVerdict(
    rubric_version="v1",
    task_class="code_generation",
    outcome="UNDETERMINED",
    undetermined_criteria=("tests_executed",),
)


def _accepted(
    score: float,
    bar: float | None,
    rubric: ModelAttemptRubricVerdict | None = None,
) -> tuple[EnumDelegationAcceptanceDecision, EnumDelegationAcceptanceReason, str]:
    return derive_attempt_acceptance(
        quality_passed=True,
        pre_filter_rejected=False,
        gate_passed=True,
        quality_score=score,
        required_bar=bar,
        rule_evaluations=(),
        rubric_verdict=rubric,
    )


def test_ac1_below_bar_score_is_not_accepted_as_quality_bar_met() -> None:
    decision, reason, detail = _accepted(0.733, 0.850)
    assert reason is not EnumDelegationAcceptanceReason.QUALITY_BAR_MET
    assert not (
        decision is EnumDelegationAcceptanceDecision.ACCEPT
        and reason is EnumDelegationAcceptanceReason.QUALITY_BAR_MET
    )
    assert "score_vs_bar=below_bar" in detail


def test_at_or_above_bar_still_reads_quality_bar_met() -> None:
    decision, reason, _ = _accepted(0.850, 0.850)
    assert decision is EnumDelegationAcceptanceDecision.ACCEPT
    assert reason is EnumDelegationAcceptanceReason.QUALITY_BAR_MET


def test_ac2_undetermined_rubric_is_named_in_the_acceptance_reason() -> None:
    _, _, detail = _accepted(0.900, 0.850, _UNDETERMINED)
    assert "rubric=UNDETERMINED" in detail
    assert "tests_executed" in detail


def test_ac3_replay_of_the_2026_11_26_receipt_fields() -> None:
    decision, reason, detail = _accepted(0.733, 0.850, _UNDETERMINED)
    assert reason is not EnumDelegationAcceptanceReason.QUALITY_BAR_MET
    assert decision is EnumDelegationAcceptanceDecision.ACCEPT
    assert reason is EnumDelegationAcceptanceReason.EXECUTED_CHECKS_PASSED
    assert detail.startswith(
        "actual_score=0.733 required_bar=0.850 score_vs_bar=below_bar"
    )
    assert "rubric=UNDETERMINED" in detail


def test_ac1_existing_call_shape_without_a_rubric_verdict() -> None:
    _, reason, _ = derive_attempt_acceptance(
        quality_passed=True,
        pre_filter_rejected=False,
        gate_passed=True,
        quality_score=0.733,
        required_bar=0.850,
        rule_evaluations=(),
    )
    assert reason is not EnumDelegationAcceptanceReason.QUALITY_BAR_MET
