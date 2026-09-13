# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18295 — one declared rule decides, and the receipt says which.

Cloud delegation ``ca144d1a-ea03-475f-bc81-650ccfa0495e`` (task type
``summarization``, dogfood tenant, 2026-09-13) terminalised ``failed`` with
this exact ``terminal_failure_reason``::

    acceptance_criteria_failed: actual_score=0.900 required_bar=0.800
    score_vs_bar=at_or_above_bar authority_source=task_class:summarization
    score_source=quality_gate_graded_score; failures=WEAK_OUTPUT: response is
    not concise

The 0.900 is not a coincidence and not a rounding artefact. ``summarization``
declares one deterministic check and four heuristic ones, and the bands are
weighted 0.6/0.4::

    (0.6 x 1/1) + (0.4 x 3/4) = 0.900

That is arithmetically "everything passed except ``concise``". So the concise
failure was ALREADY fully priced into the score — and the 0.800 bar forgave
it, saying so in the receipt with ``score_vs_bar=at_or_above_bar``. Then the
same failure was charged a second time, as an absolute boolean veto that no
bar can forgive, and the veto won.

One axis, two rules, opposite verdicts. This module pins the fix:

* a rule that contributes to the score does not ALSO veto — the bar decides
  (``TestTheBarDecidesForScoredRules``);
* a rule that vetoes is a declared floor, not a quality gradient
  (``TestBlockingRulesStillVeto``);
* every threshold lives in the contract, not in handler code
  (``TestThresholdsAreContractDeclared``);
* the receipt carries each rule's threshold and verdict beside the score, so
  the numbers read consistently together (``TestReceiptNamesTheDecidingRule``).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.inference.task_class_authority import (
    resolve_quality_rule,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

# The bar ``summarization`` declares (task_class_contracts.v1.yaml).
_SUMMARIZATION_BAR = 0.8

# The recorded score of the delegation this ticket is about.
_RECORDED_SCORE = 0.900


def _long_but_good_summary() -> str:
    """A ~400-word body: over the concise threshold, sound on every other axis.

    Mirrors the delegation's real output — a usable ticket body that a human
    had to know to dig out from under a ``failed`` status. Deliberately free
    of refusal phrasing and accuracy disclaimers so that ``concise`` is the
    ONLY rule it trips.
    """
    sentence = (
        "The projection writer records the terminal event and the consumer "
        "advances its offset once the row is durable. "
    )
    body = sentence * 30
    assert len(body.split()) > 250, "fixture must exceed the concise threshold"
    return body


def _gate_input(
    content: str, task_type: str = "summarization"
) -> ModelQualityGateInput:
    deterministic, heuristic = resolve_task_class_dod_checks(task_type)
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type=task_type,
        llm_response_content=content,
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
    )


class TestTheBarDecidesForScoredRules:
    """AC1 + AC2. A score at or above the bar is not overturned by the same axis."""

    def test_the_recorded_delegation_no_longer_fails(self) -> None:
        """RED before OMN-18295. This is the live case, replayed.

        Reproduces ``ca144d1a-ea03-475f-bc81-650ccfa0495e``: a summarization
        response that trips ``concise`` and nothing else. Before the fix the
        gate returned ``passed=False``, which the orchestrator turned into a
        terminal ``failed`` carrying its own ``score_vs_bar=at_or_above_bar``.
        """
        result = quality_gate_delta(_gate_input(_long_but_good_summary()))

        assert result.quality_score >= _SUMMARIZATION_BAR, result.quality_score
        assert result.passed is True, result.failure_reasons

    def test_the_score_still_prices_the_concise_miss(self) -> None:
        """The rule is not deleted — it is priced once instead of twice.

        A ``concise`` that stopped affecting the outcome entirely would be a
        different defect: a declared rule with no consequence. The graded
        score must still drop, which is what makes a response that trips
        SEVERAL scored rules fall under the bar on its own.
        """
        concise = quality_gate_delta(_gate_input(_long_but_good_summary()))
        brief = quality_gate_delta(
            _gate_input(
                "The writer records the terminal event, then the consumer "
                "advances its offset once the row is durable."
            )
        )
        assert concise.quality_score < brief.quality_score

    def test_the_recorded_score_is_reproduced_exactly(self) -> None:
        """AC4. The arithmetic, pinned, so the diagnosis cannot rot.

        0.900 is (0.6 x 1/1) + (0.4 x 3/4) — one deterministic check passing,
        three of four heuristics passing. If this number ever moves, the
        explanation above is wrong and this module's premise needs re-reading.
        """
        result = quality_gate_delta(_gate_input(_long_but_good_summary()))
        assert result.quality_score == pytest.approx(_RECORDED_SCORE)

    def test_enough_scored_failures_still_fall_under_the_bar(self) -> None:
        """Removing the veto must not make the gate unable to reject."""
        result = quality_gate_delta(
            _gate_input("nope.", task_type="research"),
        )
        assert result.quality_score < _SUMMARIZATION_BAR, result.quality_score


class TestBlockingRulesStillVeto:
    """A floor is not a gradient. Refusals and empties do not get a score debate."""

    def test_a_refusal_is_still_rejected(self) -> None:
        result = quality_gate_delta(_gate_input("I cannot help with that request."))
        assert result.passed is False
        assert any("REFUSAL" in r for r in result.failure_reasons), (
            result.failure_reasons
        )

    def test_an_empty_response_is_still_rejected(self) -> None:
        result = quality_gate_delta(_gate_input(""))
        assert result.passed is False

    def test_an_accuracy_disclaimer_is_still_rejected(self) -> None:
        """A response that disclaims its own accuracy is unusable, not merely weak."""
        result = quality_gate_delta(
            _gate_input(
                "This summary may be inaccurate and should not be relied upon. "
                + _long_but_good_summary()
            )
        )
        assert result.passed is False


class TestThresholdsAreContractDeclared:
    """AC2. Every rule's threshold is in the contract, not in handler code."""

    def test_concise_declares_its_threshold(self) -> None:
        """RED before OMN-18295: 250 was a literal in ``_check_concise``."""
        rule = resolve_quality_rule("concise")
        assert rule is not None, "concise must be a declared rule"
        assert rule.threshold == 250

    def test_every_rule_declares_its_enforcement(self) -> None:
        for name in ("concise", "no_refusal", "accurate", "semantic_adequacy"):
            rule = resolve_quality_rule(name)
            assert rule is not None, name
            assert rule.enforcement in ("blocking", "scored"), (name, rule.enforcement)

    def test_concise_is_scored_and_no_refusal_is_blocking(self) -> None:
        """The two classes, named. This pairing IS the fix."""
        concise = resolve_quality_rule("concise")
        refusal = resolve_quality_rule("no_refusal")
        assert concise is not None
        assert refusal is not None
        assert concise.enforcement == "scored"
        assert refusal.enforcement == "blocking"


class TestReceiptNamesTheDecidingRule:
    """AC3. Score, threshold and verdict must read consistently together."""

    def test_every_evaluated_rule_appears_on_the_result(self) -> None:
        """RED before OMN-18295: the receipt carried only free-text failures."""
        result = quality_gate_delta(_gate_input(_long_but_good_summary()))
        evaluated = {r.rule for r in result.rule_evaluations}
        deterministic, heuristic = resolve_task_class_dod_checks("summarization")
        assert evaluated == set(deterministic) | set(heuristic), evaluated

    def test_the_failing_rule_carries_its_threshold_and_verdict(self) -> None:
        result = quality_gate_delta(_gate_input(_long_but_good_summary()))
        concise = next(r for r in result.rule_evaluations if r.rule == "concise")
        assert concise.passed is False
        assert concise.threshold == 250
        assert concise.enforcement == "scored"
        assert concise.detail is not None

    def test_a_passing_rule_is_recorded_too(self) -> None:
        """A record that only exists on failure cannot distinguish pass from unrun."""
        result = quality_gate_delta(_gate_input(_long_but_good_summary()))
        refusal = next(r for r in result.rule_evaluations if r.rule == "no_refusal")
        assert refusal.passed is True
        assert refusal.detail is None

    def test_no_scored_rule_failure_is_reported_as_a_blocking_reason(self) -> None:
        """The receipt must not list a scored miss where a veto is expected.

        ``failure_reasons`` is what the orchestrator renders into the terminal
        ``failures=`` suffix. A scored rule that did not decide anything
        appearing there is exactly how a reader concluded "not concise" had
        failed the run when the bar had already forgiven it.
        """
        result = quality_gate_delta(_gate_input(_long_but_good_summary()))
        assert result.passed is True
        assert result.failure_reasons == (), result.failure_reasons


class TestTerminalReasonNamesTheDecider:
    """AC2, at the surface a customer actually reads.

    ``_score_vs_bar_reason`` composes the ``terminal_failure_reason`` that
    lands on the receipt. OMN-15464 already made its LABEL three-way, so the
    0.900 delegation was at least called ``acceptance_criteria_failed`` rather
    than the flat lie ``score_below_required_bar``. What it still did not say
    was WHICH criterion — that was left to a free-text fragment after
    ``failures=``, which is also where a scored rule's harmless miss used to
    appear, indistinguishable from a real decider.
    """

    def test_a_blocking_failure_is_named_in_the_terminal_reason(self) -> None:
        from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
            HandlerDelegationWorkflow,
        )

        result = quality_gate_delta(_gate_input("I cannot help with that request."))
        authority = _RequiredBarAuthorityStub()
        reason = HandlerDelegationWorkflow._score_vs_bar_reason(
            result, authority, pre_filter_rejected=False
        )
        assert "deciding_rules=" in reason, reason
        assert "no_refusal" in reason, reason

    def test_a_scored_miss_never_appears_as_a_decider(self) -> None:
        """The exact combination from the ticket, read back off the receipt."""
        from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
            HandlerDelegationWorkflow,
        )

        result = quality_gate_delta(_gate_input(_long_but_good_summary()))
        reason = HandlerDelegationWorkflow._score_vs_bar_reason(
            result, _RequiredBarAuthorityStub(), pre_filter_rejected=False
        )
        assert "deciding_rules=" not in reason, reason
        assert "concise" not in reason, reason


class _RequiredBarAuthorityStub:
    """The two fields ``_score_vs_bar_reason`` reads, and nothing else."""

    required_bar = _SUMMARIZATION_BAR
    authority_source = "task_class:summarization"
    score_source = "quality_gate_graded_score"
