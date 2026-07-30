# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15464: the delegation terminal must not describe a passing score as below the bar.

Reproduced live on 2026-07-30 by a read-only consume of the tenant-prefixed dev MSK
terminal topic
``tenant-beta-gateway-canary-79afa7263852.onex.evt.omnibase-infra.delegation-failed.v1``
(offset 0, hw=1) for the Hybrid Gateway canary correlation
``8371bb34-3aa4-48d6-bdce-dffae3eb4b7f``. The durable payload carried::

    "failure_reason": "score_below_required_bar: actual_score=0.867 required_bar=0.800
     authority_source=task_class:reasoning score_source=quality_gate_graded_score;
     failures=TASK_MISMATCH: failed step_by_step_explanation"

0.867 is ABOVE 0.800, so the verdict label contradicts its own printed numbers. The
actual rejection cause was the ``step_by_step_explanation`` acceptance criterion
(``result.passed is False``), which is an independent gate leg from the numeric bar.

``handle_gate_result`` rejects on three independent causes (deterministic pre-filter /
criterion failure / genuine sub-bar score) but ``_score_vs_bar_reason`` emitted only a
BINARY label, so the middle cause was always mislabelled as the third.

The same payload also proved an attempt undercount: ``attempts_count=2`` while the
``escalation_history`` in that very payload held FOUR rejected attempts (3x tier
``local`` + 1x ``cheap_cloud``). ``attempts_count`` was derived as
``escalation_count + 1``, and OMN-14234 same-tier retries deliberately do not bump
``escalation_count``, so every same-tier retry was invisible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_escalation_attempt import (
    ModelDelegationEscalationAttempt,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
    RequiredBarAuthority,
    resolve_required_bar_authority,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)

# The exact values read off the live terminal.
OBSERVED_SCORE = 0.867
OBSERVED_BAR = 0.800
OBSERVED_CRITERION_FAILURE = "TASK_MISMATCH: failed step_by_step_explanation"
SUB_BAR_LABEL = "score_below_required_bar"


def _authority(
    required_bar: float = OBSERVED_BAR,
    *,
    authority_source: str = "task_class:reasoning",
    score_source: str = "quality_gate_graded_score",
) -> RequiredBarAuthority:
    return RequiredBarAuthority(
        required_bar=required_bar,
        authority_source=authority_source,
        score_source=score_source,
    )


def _gate_result(
    *,
    quality_score: float,
    passed: bool,
    failure_reasons: tuple[str, ...],
    correlation_id: UUID | None = None,
) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=correlation_id or uuid4(),
        passed=passed,
        quality_score=quality_score,
        failure_reasons=failure_reasons,
        fallback_recommended=not passed,
    )


def _reason(
    result: ModelQualityGateResult,
    authority: RequiredBarAuthority,
    *,
    pre_filter_rejected: bool = False,
) -> str:
    return HandlerDelegationWorkflow._score_vs_bar_reason(
        result,
        authority,
        pre_filter_rejected=pre_filter_rejected,
    )


def _verdict_label(reason: str) -> str:
    """The verdict label is the token before the first ``:`` separator.

    Asserting on the LABEL rather than on ``"below" in reason`` matters: the
    free-text ``failures=`` suffix legitimately contains the word "below" for
    ``WEAK_OUTPUT: response length N below minimum M``, so a naive substring
    check would false-positive.
    """
    return reason.split(":", 1)[0].strip()


@pytest.mark.unit
class TestVerdictLabelIsTruthful:
    """A score at or above the bar must never be labelled as below it."""

    def test_observed_canary_case_is_not_labelled_sub_bar(self) -> None:
        """RED before OMN-15464: the exact 0.867 / 0.800 live payload."""
        result = _gate_result(
            quality_score=OBSERVED_SCORE,
            passed=False,
            failure_reasons=(OBSERVED_CRITERION_FAILURE,),
        )

        reason = _reason(result, _authority())

        assert OBSERVED_SCORE > OBSERVED_BAR, "fixture guard: the score clears the bar"
        assert _verdict_label(reason) != SUB_BAR_LABEL, (
            "0.867 is above the 0.800 bar; the terminal must not label this "
            f"{SUB_BAR_LABEL!r}. Got: {reason!r}"
        )
        # The real cause must be named, not buried in a free-text suffix.
        assert "criteria" in _verdict_label(reason), (
            f"the verdict label must name the acceptance-criteria failure. Got: {reason!r}"
        )
        assert "step_by_step_explanation" in reason

    def test_comparison_outcome_is_an_explicit_field(self) -> None:
        """The score-vs-bar comparison must be readable without inferring it."""
        result = _gate_result(
            quality_score=OBSERVED_SCORE,
            passed=False,
            failure_reasons=(OBSERVED_CRITERION_FAILURE,),
        )

        reason = _reason(result, _authority())

        assert "score_vs_bar=at_or_above_bar" in reason, (
            f"expected an explicit truthful comparison token. Got: {reason!r}"
        )
        assert f"actual_score={OBSERVED_SCORE:.3f}" in reason
        assert f"required_bar={OBSERVED_BAR:.3f}" in reason

    def test_genuine_sub_bar_still_reports_sub_bar(self) -> None:
        """No OMN-13368 regression: a real sub-bar score keeps its label."""
        result = _gate_result(
            quality_score=0.500,
            passed=False,
            failure_reasons=("TASK_MISMATCH: missing markers",),
        )

        reason = _reason(result, _authority())

        assert _verdict_label(reason) == SUB_BAR_LABEL
        assert "score_vs_bar=below_bar" in reason

    def test_deterministic_pre_filter_keeps_precedence(self) -> None:
        """A deterministic-floor rejection still wins the label."""
        result = _gate_result(
            quality_score=0.0,
            passed=False,
            failure_reasons=("MALFORMED: response is not valid JSON",),
        )

        reason = _reason(result, _authority(), pre_filter_rejected=True)

        assert _verdict_label(reason) == "pre_filter_rejected"
        # ...but the comparison token still tells the truth about the numbers.
        assert "score_vs_bar=below_bar" in reason

    @pytest.mark.parametrize(
        ("score", "bar", "pre_filter"),
        [
            (0.867, 0.800, False),  # the live canary case
            (0.800, 0.800, False),  # exactly at the bar is NOT below it
            (0.801, 0.800, False),
            (0.500, 0.800, False),  # genuinely below
            (0.000, 0.800, True),  # deterministic rejection
            (0.900, 0.850, False),
        ],
    )
    def test_sub_bar_label_implies_score_is_actually_below_bar(
        self, score: float, bar: float, pre_filter: bool
    ) -> None:
        """Invariant: the sub-bar LABEL may only appear when score < bar."""
        result = _gate_result(
            quality_score=score,
            passed=False,
            failure_reasons=("TASK_MISMATCH: failed step_by_step_explanation",),
        )

        reason = _reason(result, _authority(bar), pre_filter_rejected=pre_filter)

        if _verdict_label(reason) == SUB_BAR_LABEL:
            assert score < bar, (
                f"{SUB_BAR_LABEL!r} emitted for score={score} bar={bar}, "
                f"which is not below the bar. Got: {reason!r}"
            )
        expected_token = (
            "score_vs_bar=below_bar" if score < bar else "score_vs_bar=at_or_above_bar"
        )
        assert expected_token in reason

    def test_boundary_equal_score_is_not_below_bar(self) -> None:
        """`>=` acceptance means an exactly-at-bar score is never 'below'."""
        result = _gate_result(
            quality_score=OBSERVED_BAR,
            passed=False,
            failure_reasons=(OBSERVED_CRITERION_FAILURE,),
        )

        reason = _reason(result, _authority())

        assert _verdict_label(reason) != SUB_BAR_LABEL
        assert "score_vs_bar=at_or_above_bar" in reason

    def test_failed_criteria_are_enumerated(self) -> None:
        """Every failed criterion survives onto the reason, not just the first."""
        result = _gate_result(
            quality_score=OBSERVED_SCORE,
            passed=False,
            failure_reasons=(
                OBSERVED_CRITERION_FAILURE,
                "TASK_MISMATCH: failed plain_text_only",
            ),
        )

        reason = _reason(result, _authority())

        assert "step_by_step_explanation" in reason
        assert "plain_text_only" in reason


@pytest.mark.unit
class TestReasoningTaskClassBarIsTheObservedOne:
    """Guard the fixture against contract drift."""

    def test_reasoning_required_bar_is_point_eight(self) -> None:
        authority = resolve_required_bar_authority(task_type="reasoning")
        assert authority.required_bar == pytest.approx(OBSERVED_BAR)
        assert authority.required_bar < OBSERVED_SCORE


def _workflow(
    *, escalation_count: int, tiers: tuple[str, ...]
) -> DelegationWorkflowState:
    correlation_id = uuid4()
    workflow = DelegationWorkflowState(
        correlation_id=correlation_id,
        request=ModelDelegationRequest(
            prompt="Return exactly: HYBRID_GATEWAY_CANARY",
            task_type="reasoning",  # type: ignore[arg-type]
            correlation_id=correlation_id,
            emitted_at=datetime.now(UTC),
        ),
    )
    workflow.escalation_count = escalation_count
    workflow.escalation_history = [_attempt(tier) for tier in tiers]
    return workflow


@pytest.mark.unit
class TestAttemptsCountCountsEveryAttempt:
    """`attempts_count` must not silently drop same-tier retries."""

    def test_same_tier_retries_are_counted(self) -> None:
        """The live canary made 4 attempts but the terminal claimed 2."""
        # Exactly the live shape: 1 tier escalation (local -> cheap_cloud) but
        # FOUR recorded attempts, because OMN-14234 retried the local tier.
        workflow = _workflow(
            escalation_count=1,
            tiers=("local", "local", "local", "cheap_cloud"),
        )

        counted = HandlerDelegationWorkflow._truthful_attempts_count(
            workflow, completed=False
        )

        assert counted == 4, (
            "the terminal must report the 4 attempts its own escalation_history "
            f"proves, not escalation_count+1=2. Got {counted}."
        )

    def test_completed_path_counts_the_accepted_attempt(self) -> None:
        """Two rejected attempts, then acceptance on the third."""
        workflow = _workflow(escalation_count=1, tiers=("local", "cheap_cloud"))

        assert (
            HandlerDelegationWorkflow._truthful_attempts_count(workflow, completed=True)
            == 3
        )

    def test_first_attempt_success_is_one(self) -> None:
        workflow = _workflow(escalation_count=0, tiers=())

        assert (
            HandlerDelegationWorkflow._truthful_attempts_count(workflow, completed=True)
            == 1
        )

    def test_never_reports_zero_attempts(self) -> None:
        """A terminal with no recorded history still made one attempt."""
        workflow = _workflow(escalation_count=0, tiers=())

        assert (
            HandlerDelegationWorkflow._truthful_attempts_count(
                workflow, completed=False
            )
            >= 1
        )

    def test_never_regresses_below_the_escalation_derived_floor(self) -> None:
        """A history-less escalated workflow keeps the old escalation_count+1."""
        workflow = _workflow(escalation_count=2, tiers=())

        assert (
            HandlerDelegationWorkflow._truthful_attempts_count(
                workflow, completed=False
            )
            == 3
        )


def _attempt(tier_name: str) -> ModelDelegationEscalationAttempt:
    return ModelDelegationEscalationAttempt(
        tier_name=tier_name,
        model_used="Qwen3.6-35B-A3B",
        quality_score=OBSERVED_SCORE,
        required_bar=OBSERVED_BAR,
        actual_score=OBSERVED_SCORE,
        authority_source="task_class:reasoning",
        score_source="quality_gate_graded_score",
        failure_reasons=(OBSERVED_CRITERION_FAILURE,),
        latency_ms=1958,
        fallback_recommended=True,
        attempted_at=datetime.now(UTC),
        routing_decision_id=uuid4(),
    )
