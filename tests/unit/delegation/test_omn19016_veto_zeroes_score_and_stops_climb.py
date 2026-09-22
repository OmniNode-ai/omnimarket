# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19016 — a veto zeroes the field it overrides, and does not climb rungs it cannot change.

The fixture is a real terminal, not a hand-written one:
``tests/fixtures/receipts/omn19016_ready_probe_f037b9be.json`` is the recorded
subset of run ``11dbb397-eb1b-4fa5-b5f2-a70a10e0ae10`` (correlation
``f037b9be-b242-4b83-9912-3e6a5af83e82``, dev lane, deployed locus). The prompt
asked for exactly one word, the model answered ``READY``, and the terminal says
both of these at once:

    quality_score            0.867      score_vs_bar=at_or_above_bar
    status                   failed     deciding_rules=semantic_adequacy

A consumer reading the score concludes pass; a consumer reading the terminal
concludes fail. Four rungs were attempted, one of them metered, and every rung
returned the identical score and the identical refusal — the veto is a function
of the response's SHAPE, so no costlier rung could ever have satisfied it.

Two properties are proven here, each RED against that transcript first:

1. a blocking rule that vetoes acceptance ZEROES the score it overrides, the way
   the OMN-18278 truncation floor and the OMN-18967 unresolved-preamble floor
   already do, so no record reports a score at or above the bar beside a
   failure;
2. a veto that is a deterministic function of the response's shape TERMINALISES
   instead of climbing, while a genuinely weak answer — empty, truncated
   mid-token, truncated mid-clause — still climbs, because a costlier rung can
   cure those and cannot cure a shape.

Deliberately NOT asserted here: the OMN-13642 judge veto, which records a
supra-bar combined score beside a FAIL verdict on purpose ("the rejection is the
verdict veto, not a score drop"). That veto is a model verdict rather than a
deterministic rule, it is not one of the sibling floors this ticket matches, and
reconciling it with the standing terminal invariant is OMN-19004's work.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
    resolve_required_bar_authority,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_requested_shape_for_prompt,
)

pytestmark = pytest.mark.unit

# The verdict prefix a shape refusal carries, spelled as a literal rather than
# imported from the handler ON PURPOSE. Importing the new name would make this
# module fail to COLLECT against pre-fix source, and a collection error is not
# a red test — it proves the symbol is absent, not that the defect is present.
# Spelled here, every assertion below runs against unfixed source and fails on
# the measured behaviour instead.
SHAPE_REFUSED_VERDICT_PREFIX = "SHAPE_REFUSED"

RECEIPT_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "receipts"
    / "omn19016_ready_probe_f037b9be.json"
)


def _receipt() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(RECEIPT_FIXTURE.read_text(encoding="utf-8"))
    return payload


def _gate_input_from_receipt(content: str | None = None) -> ModelQualityGateInput:
    """The gate input the recorded run actually presented, replayed verbatim."""
    request = _receipt()["request"]
    return ModelQualityGateInput(
        correlation_id=str(uuid.uuid4()),
        task_type=request["task_type"],
        llm_response_content=(
            request["llm_response_content"] if content is None else content
        ),
        dod_deterministic=tuple(request["dod_deterministic"]),
        dod_heuristic=tuple(request["dod_heuristic"]),
    )


def _failing_blocking_rules(result: Any) -> tuple[str, ...]:
    return tuple(
        evaluation.rule
        for evaluation in result.rule_evaluations
        if not evaluation.passed and evaluation.enforcement == "blocking"
    )


# ---------------------------------------------------------------------------
# The captured transcript is the defect, before any claim about the code.
# ---------------------------------------------------------------------------


def test_captured_terminal_contradicts_its_own_attempt_records() -> None:
    """The recorded run asserts at_or_above_bar and failed in the same record."""
    receipt = _receipt()
    terminal = receipt["terminal"]
    request = receipt["request"]

    assert terminal["status"] == "failed"
    assert terminal["quality_gate_passed"] is False
    assert terminal["quality_score"] >= request["required_bar"]
    assert any(
        "at_or_above_bar" in reason for reason in terminal["quality_gates_failed"]
    )


def test_captured_run_climbed_four_rungs_to_the_identical_refusal() -> None:
    """Every rung returned the same score and the same refusal, one of them metered."""
    attempts = _receipt()["attempts"]

    assert len(attempts) == 4
    assert {attempt["acceptance_decision"] for attempt in attempts} == {"climb"}
    assert len({attempt["quality_score"] for attempt in attempts}) == 1
    assert len({attempt["error_message"] for attempt in attempts}) == 1
    assert any(attempt["tier"] != attempts[0]["tier"] for attempt in attempts)


# ---------------------------------------------------------------------------
# 1. A veto zeroes the field it overrides and names the rule that decided.
# ---------------------------------------------------------------------------


def test_veto_zeroes_the_score_it_overrides() -> None:
    """RED against f037b9be: the veto stood beside a 0.867 at-or-above-bar score."""
    result = delta(_gate_input_from_receipt())

    assert result.passed is False
    assert result.quality_score == pytest.approx(0.0)
    assert _failing_blocking_rules(result) == ("semantic_adequacy",)


def test_no_vetoed_result_reports_a_score_at_or_above_the_bar() -> None:
    """The invariant, over every response a blocking rule of this class vetoes.

    The empty response is deliberately absent: it fails ``response_non_empty``,
    a DECLARED DETERMINISTIC check, and that band keeps the OMN-12964 graded
    fraction by design so experiment analysis can tell a near-miss from a total
    failure. It is below the bar here and cannot be lifted over it by any
    score, but the general case of a multi-check deterministic band reading at
    or above the bar beside a failure is the terminal-level invariant, which is
    OMN-19004's surface, not a second copy of it here.
    """
    required_bar = _receipt()["request"]["required_bar"]
    vetoed_responses = (
        "READY",
        "The change adds a graded score so the",
        "The summary is truncated mid-token,",
        "I cannot help with that.",
    )

    for content in vetoed_responses:
        result = delta(_gate_input_from_receipt(content))
        assert result.passed is False, content
        assert result.fail_category == "fail_heuristic", content
        assert result.quality_score < required_bar, content
        assert result.quality_score == pytest.approx(0.0), content


def test_the_composed_terminal_reason_no_longer_claims_at_or_above_bar() -> None:
    """AC1, asserted on the exact string the captured receipt published.

    The receipt's single ``quality_gates_failed`` entry is not the gate's raw
    reason list. It is composed by the orchestrator, which prints the
    comparison as its own ``score_vs_bar=`` token so a consumer never has to
    infer it from the label. That composition is a SECOND derivation from the
    score, so zeroing the score in the gate is only half the repair unless the
    two stayed wired together — this asserts they did, against the real
    ``document`` bar resolved from the production contract rather than a
    number written here.

    Recorded on run ``11dbb397-eb1b-4fa5-b5f2-a70a10e0ae10``::

        acceptance_criteria_failed: actual_score=0.867 required_bar=0.800
        score_vs_bar=at_or_above_bar ... deciding_rules=semantic_adequacy

    A reader of that token concluded the answer cleared the bar; the same
    record's terminal said ``failed``.
    """
    receipt = _receipt()
    result = delta(_gate_input_from_receipt())
    authority = resolve_required_bar_authority(
        task_type=receipt["request"]["task_type"]
    )

    # The bar the live run was judged against, confirmed from the contract and
    # not restated from the fixture.
    assert authority.required_bar == pytest.approx(receipt["request"]["required_bar"])

    reason = HandlerDelegationWorkflow._score_vs_bar_reason(
        result,
        authority,
        pre_filter_rejected=False,
    )

    assert "score_vs_bar=at_or_above_bar" not in reason, reason
    assert "score_vs_bar=below_bar" in reason, reason
    assert "actual_score=0.000" in reason, reason
    assert "semantic_adequacy" in reason, reason


def test_a_passing_response_still_reports_at_or_above_bar() -> None:
    """Positive control: the token is not hard-wired to below_bar by the fix.

    Without this, zeroing every score would satisfy the assertion above and
    destroy the field's meaning. A response the gate ACCEPTS must still print
    the comparison that describes it.
    """
    result = delta(
        _gate_input_from_receipt(
            "The delegation ladder walks the tiers a task class declares, in "
            "order, stopping at the first rung whose answer the gate accepts."
        )
    )
    authority = resolve_required_bar_authority(
        task_type=_receipt()["request"]["task_type"]
    )

    assert result.passed is True

    reason = HandlerDelegationWorkflow._score_vs_bar_reason(
        result,
        authority,
        pre_filter_rejected=False,
    )

    assert "score_vs_bar=at_or_above_bar" in reason, reason


# ---------------------------------------------------------------------------
# 2. A shape veto stops the ladder; a weak answer still climbs.
# ---------------------------------------------------------------------------


def test_shape_veto_does_not_climb() -> None:
    """RED against f037b9be: the shape refusal recommended a costlier rung."""
    result = delta(_gate_input_from_receipt())

    assert result.passed is False
    assert result.fallback_recommended is False
    assert result.no_rung_can_satisfy is True
    assert any(
        reason.startswith(SHAPE_REFUSED_VERDICT_PREFIX)
        for reason in result.failure_reasons
    )


def test_a_climbable_reason_beside_a_shape_veto_keeps_the_climb() -> None:
    """One curable objection is enough to make a costlier rung worth buying.

    ``no`` is a single word AND a content-free declination, so it trips the
    shape rule and ``no_refusal`` together. The refusal half is exactly what a
    stronger model recovers from, so the run must keep its escalation — the
    futility verdict is ``all``, never ``any``.
    """
    result = delta(_gate_input_from_receipt("no"))

    assert result.passed is False
    assert result.no_rung_can_satisfy is False
    assert result.fallback_recommended is True


def test_a_passing_response_is_never_reported_unsatisfiable() -> None:
    """The futility flag is a property of a veto, so a pass never carries it."""
    result = delta(
        _gate_input_from_receipt(
            "The delegation ladder walks the tiers a task class declares, in "
            "order, stopping at the first rung whose answer the gate accepts."
        )
    )

    assert result.passed is True
    assert result.no_rung_can_satisfy is False
    assert result.quality_score > 0.0


# ---------------------------------------------------------------------------
# 3. Class fit, addressed after 1 and 2 and not instead of them.
# ---------------------------------------------------------------------------


def test_the_probe_prompt_declares_its_own_shape() -> None:
    """The prompt SAID one word; the directive list did not recognise the phrasing.

    OMN-16932 already reads the shape a prompt declares and swaps the class's
    adequacy authority for ``short_form_adequacy`` when it finds one. The
    captured probe went unrecognised on one missing adverb, so it was graded
    against the ``document`` prose rubric and refused for obeying its own
    instruction. This is the secondary defect the ticket orders last: with 1
    and 2 landed the run is honest either way, and with this it is also
    correct on the first rung.
    """
    prompt = _receipt()["request"]["prompt"]

    assert resolve_requested_shape_for_prompt(prompt).value == "single_word"


def test_an_unconstrained_prompt_does_not_acquire_a_shape() -> None:
    """The regression guard on the widened pattern: prose keeps its prose rubric."""
    for prompt in (
        "Summarise the delegation receipt in one paragraph.",
        "Write the runbook section explaining the escalation ladder.",
        "Explain how the gate decides in about one page of prose.",
    ):
        assert resolve_requested_shape_for_prompt(prompt).value == "unconstrained", (
            prompt
        )


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("", id="empty"),
        pytest.param("The refactor extracts the helper and(", id="truncated_mid_token"),
        pytest.param(
            "The change adds a graded score so the", id="truncated_mid_clause"
        ),
    ],
)
def test_weak_answer_still_climbs(content: str) -> None:
    """Negative control: a costlier rung can cure these, so the ladder still climbs."""
    result = delta(_gate_input_from_receipt(content))

    assert result.passed is False
    assert result.fallback_recommended is True
