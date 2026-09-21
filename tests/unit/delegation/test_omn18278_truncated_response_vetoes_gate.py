# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18278: a budget-truncated scratchpad is not a finished answer.

OMN-18379 taught the gate to cut a leaked reasoning scratchpad off the front of
an answer at the model's own unpaired trace terminator. That closes the case
OMN-18278's description names, and it is already shipped.

The case it does NOT close is the one lane ``delegation-dogfood-0045``
reproduced four times on 2026-09-16 (runs 1, 3, 10, 11): when the output-token
budget runs out BEFORE the model closes its reasoning block, the terminator is
never emitted, so there is no boundary to cut at and no answer was ever
generated. The caller receives the scratchpad alone. Every textual heuristic
reads it as well-formed prose, so the run terminalises ``completed`` with
``quality_gate_passed=true`` at a score of ``1.0``. Attaching the two declared
criteria written for exactly this shape -- ``final_artifact_only`` and
``response_non_empty`` -- did not change the verdict.

The response had said so all along: ``choices[0].finish_reason == "length"``.
The sibling effect handler ``handler_inference_intent`` already refuses a
provider call on that field. The handler the bus-less local path actually runs
never read it, and the gate had no parameter it could have arrived through.

These tests pin the three properties that follow:

* the gate VETOES a response the provider reported as truncated, whatever the
  content heuristics think of the text;
* a normal response is byte-unchanged -- the veto is not a new way to fail
  ordinary work;
* the truncation predicate is ONE implementation, shared with the sibling effect
  path, rather than a second copy of ``== "length"``.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.delegation.reasoning_preamble import (
    UNRESOLVED_PREAMBLE_CHECK_NAME,
    EnumReasoningBoundaryRule,
    segment_reasoning_preamble,
)
from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.inference.provider_finish_reason import (
    TRUNCATED_RESPONSE_ERROR_MESSAGE,
    TRUNCATION_CHECK_NAME,
    finish_reason_from_choice,
    is_truncated_by_output_budget,
    parse_finish_reason,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    _terminal_artifact,
    derive_attempt_acceptance,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

#: The `document` class DoD, verbatim from configs/task_class_contracts.v1.yaml.
#: `identifiers_grounded` is omitted: it is evaluated only when the caller
#: supplies a grounding source, which these inputs do not carry.
_DOCUMENT_DETERMINISTIC = ("response_non_empty",)
_DOCUMENT_HEURISTIC = ("no_refusal", "accurate", "semantic_adequacy")

_CORRELATION_ID = UUID("b0b21a44-fed1-414e-853e-8324532c8b94")

#: A scratchpad that was cut off by the output budget mid-thought.
#:
#: It opens with a declared lead-in phrase and carries NO closing trace tag, no
#: ATX header and no fence -- so no declared boundary resolves.
#:
#: UPDATED 2026-09-21 (OMN-18967 AC3). The segmenter used to report
#: ``no_boundary_found`` here, the same value a clean answer carries, and this
#: comment recorded that as correct. It now reports ``preamble_unresolved``,
#: because a declared lead-in opened the response and nothing was found behind
#: it, and the gate refuses on that alone. Every sentence is still complete and
#: the prose still ordinary, so the CONTENT CHECKS still accept it -- what
#: changed is that a boundary-level floor now runs ahead of them. A truncated
#: response that does NOT announce itself with a lead-in is still caught only
#: by the provider's own ``finish_reason``; see
#: ``test_the_truncation_veto_still_does_work_the_content_path_cannot``.
_TRUNCATED_SCRATCHPAD = (
    "Okay, the user wants a short paragraph describing what the delegation "
    "quality gate does and why the local tier exists. Let me work out what "
    "belongs in it before I write anything.\n\n"
    "The delegation quality gate sits after the inference call and before the "
    "result is accepted. It runs the deterministic checks the task class "
    "declares, then combines a judge adequacy score for classes that allow "
    "one. So the paragraph should mention both halves.\n\n"
    "I should also say something about the local tier. The local tier is the "
    "cheapest rung on the escalation ladder, so the gate is what decides "
    "whether the ladder stops there or climbs. That is the interesting part "
    "and it belongs in the paragraph too.\n\n"
    "One more consideration. The user asked for a single paragraph, so I need "
    "to keep this tight and avoid a bulleted list. I will open with the gate "
    "itself and close with the escalation consequence."
)

#: The same request, answered. Complete prose, no scratchpad, provider reported
#: a normal stop.
_COMPLETE_ANSWER = (
    "The delegation quality gate runs immediately after the inference call and "
    "before any result is accepted: it evaluates the deterministic checks the "
    "task class declares, combines an LLM-judge adequacy score for the classes "
    "that permit one, and returns a single verdict with the graded score that "
    "produced it. Because the local tier is the cheapest rung on the "
    "escalation ladder, that verdict is what decides whether a delegation "
    "stops at the local model or climbs to a paid one, which makes the gate "
    "the control point for both cost and correctness."
)


def _gate_input(content: str) -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=_CORRELATION_ID,
        task_type="document",
        llm_response_content=content,
        dod_deterministic=_DOCUMENT_DETERMINISTIC,
        dod_heuristic=_DOCUMENT_HEURISTIC,
    )


# ---------------------------------------------------------------------------
# The shared predicate — one implementation, not two
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("length", EnumProviderFinishReason.LENGTH),
        ("LENGTH", EnumProviderFinishReason.LENGTH),
        ("stop", EnumProviderFinishReason.STOP),
        ("content_filter", EnumProviderFinishReason.CONTENT_FILTER),
        ("tool_calls", EnumProviderFinishReason.TOOL_CALLS),
        (None, EnumProviderFinishReason.ABSENT),
        ("some_new_vendor_value", EnumProviderFinishReason.UNRECOGNISED),
        (7, EnumProviderFinishReason.UNRECOGNISED),
    ],
)
def test_finish_reason_parses_without_guessing(
    raw: object, expected: EnumProviderFinishReason
) -> None:
    """A missing reason and an unmodelled reason stay DIFFERENT facts."""
    assert parse_finish_reason(raw) is expected


def test_only_an_explicit_length_counts_as_truncation() -> None:
    """Silence is not evidence of truncation, and it is not evidence of a finish."""
    assert is_truncated_by_output_budget(EnumProviderFinishReason.LENGTH)
    for reason in EnumProviderFinishReason:
        if reason is EnumProviderFinishReason.LENGTH:
            continue
        assert not is_truncated_by_output_budget(reason)


def test_choice_reader_matches_the_openai_wire_shape() -> None:
    """The reader takes a ``choices[]`` entry, the shape both handlers hold."""
    assert (
        finish_reason_from_choice({"finish_reason": "length", "message": {}})
        is EnumProviderFinishReason.LENGTH
    )
    assert finish_reason_from_choice({"message": {}}) is EnumProviderFinishReason.ABSENT


def test_sibling_effect_path_uses_this_module_not_a_second_copy() -> None:
    """The refusal ``handler_inference_intent`` raises is THIS constant.

    The two effect-boundary handlers drifted because the comparison was written
    inline in one of them. Importing the module object and asserting the
    identity of the shared constant is what makes a re-divergence a red test
    rather than a review catch.
    """
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
        handler_inference_intent,
    )

    assert (
        handler_inference_intent.TRUNCATED_RESPONSE_ERROR_MESSAGE
        is TRUNCATED_RESPONSE_ERROR_MESSAGE
    )
    # The literal the bus orchestrator's text classifier matches on to resolve
    # CONTEXT_TOO_LARGE. Pinned so a reword cannot silently reclassify.
    assert "finish_reason=length" in TRUNCATED_RESPONSE_ERROR_MESSAGE


# ---------------------------------------------------------------------------
# The defect — the gate must veto a truncated response
# ---------------------------------------------------------------------------


def test_the_truncated_scratchpad_has_no_boundary_to_strip() -> None:
    """The premise. OMN-18379's segmenter cannot help here, and says so.

    RE-JUSTIFIED 2026-09-21 (OMN-18967 AC3), which is what the sibling test
    below asks a future change to do rather than leave a stale premise
    standing. The premise is unchanged and still true: the segmenter recovers
    no answer from this response, so the truncation veto is still required.
    What changed is that it now says so with a rule of its own.

    This response opens with a declared lead-in and no boundary resolves
    behind it, so the rule is ``preamble_unresolved`` rather than
    ``no_boundary_found``. Before OMN-18967 both outcomes reported the latter,
    so "there was nothing to strip" and "the whole response is scratchpad"
    were indistinguishable — and the second is exactly this fixture.

    **The truncation veto is NOT made redundant by that refusal**, and the
    distinction matters enough to assert. ``preamble_unresolved`` is resolved
    by matching declared phrases against the text, which is a heuristic over
    content the model produced. ``finish_reason`` is what the PROVIDER said
    about the call, which the model cannot forge. A response can be truncated
    without opening with any declared phrase, and can open with one without
    being truncated. Two independent signals, neither a substitute for the
    other.
    """
    segmentation = segment_reasoning_preamble(_TRUNCATED_SCRATCHPAD)
    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.PREAMBLE_UNRESOLVED
    assert segmentation.boundary_rule is not EnumReasoningBoundaryRule.NO_BOUNDARY_FOUND
    assert segmentation.preamble == ""
    assert segmentation.answer == _TRUNCATED_SCRATCHPAD


def test_content_heuristics_alone_accept_the_scratchpad() -> None:
    """SUPERSEDED 2026-09-21 (OMN-18967 AC3). The content path now catches it.

    This test previously asserted that with no truncation signal the gate
    accepted this scratchpad at 1.0, and said in its own docstring that a
    change making the CONTENT checks catch the shape should show up here as a
    failure, so the truncation veto could be re-justified against the new
    behaviour rather than standing on a stale premise. That is what happened,
    so the premise is restated rather than patched.

    This fixture opens with `Okay, the user wants`, a declared reasoning
    lead-in, and no declared boundary resolves an answer behind it. OMN-18967
    gave that outcome its own rule, `preamble_unresolved`, and a
    class-independent gate floor. So the content path refuses it now, with no
    provider signal at all.

    **This does not retire the truncation veto**, and the test below that
    re-justifies it independently is the one to read next.
    """
    result = delta(
        _gate_input(_TRUNCATED_SCRATCHPAD),
        finish_reason=EnumProviderFinishReason.ABSENT,
    )
    assert not result.passed
    assert result.quality_score == 0.0
    rules = {evaluation.rule: evaluation for evaluation in result.rule_evaluations}
    assert UNRESOLVED_PREAMBLE_CHECK_NAME in rules
    assert not rules[UNRESOLVED_PREAMBLE_CHECK_NAME].passed


def test_the_truncation_veto_still_does_work_the_content_path_cannot() -> None:
    """The re-justification OMN-18967 owes the veto above.

    A truncated response need not announce itself. This one carries no
    declared lead-in phrase, no trace terminator and no structural boundary,
    so the segmenter reports the clean outcome and the OMN-18967 preamble
    floor never fires. It is nonetheless not a finished answer, and only the
    provider's own `finish_reason` says so.

    The claim asserted below is deliberately narrow. It is NOT that the
    content checks accept this text — a `document`-class deterministic check
    may well dislike a sentence that stops mid-clause, and pinning that would
    make this test depend on a DoD it does not own. The claim is that the two
    VETOES are independent: the preamble floor does not fire here, and the
    truncation veto is the sole rule naming the refusal when the provider
    signal is present.

    That makes the two signals independent rather than redundant: the content
    floor catches a scratchpad that announces itself, and the truncation veto
    catches an answer that simply stops. Removing either leaves a shape the
    other does not cover.
    """
    stops_mid_sentence = (
        "The delegation quality gate runs after the inference call and before "
        "the result is accepted. It evaluates the deterministic checks the "
        "task class declares, and for classes that allow one it combines a "
        "judge adequacy score. The local tier exists because it is the "
        "cheapest rung on the escalation ladder, which means the gate is what "
        "decides whether the ladder stops there or"
    )

    segmentation = segment_reasoning_preamble(stops_mid_sentence)
    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.NO_BOUNDARY_FOUND, (
        "this fixture must NOT trip the content floor, or it cannot show that "
        "the truncation veto covers a shape the content path misses"
    )

    without_the_signal = delta(
        _gate_input(stops_mid_sentence),
        finish_reason=EnumProviderFinishReason.ABSENT,
    )
    without_rules = {
        evaluation.rule for evaluation in without_the_signal.rule_evaluations
    }
    assert UNRESOLVED_PREAMBLE_CHECK_NAME not in without_rules, (
        "the OMN-18967 preamble floor fired on a response carrying no declared "
        "lead-in, so this fixture cannot show the two signals are independent"
    )
    assert TRUNCATION_CHECK_NAME not in without_rules

    refused_with_the_signal = delta(
        _gate_input(stops_mid_sentence),
        finish_reason=EnumProviderFinishReason.LENGTH,
    )
    assert not refused_with_the_signal.passed
    assert "finish_reason=length" in refused_with_the_signal.failure_reasons[0]
    assert refused_with_the_signal.quality_score == 0.0
    with_rules = {
        evaluation.rule for evaluation in refused_with_the_signal.rule_evaluations
    }
    assert with_rules == {TRUNCATION_CHECK_NAME}, (
        "the truncation veto must be the sole rule naming this refusal; the "
        "preamble floor cannot see this shape at all"
    )


def test_a_truncated_response_fails_the_gate() -> None:
    """The fix. The provider's own signal vetoes acceptance."""
    result = delta(
        _gate_input(_TRUNCATED_SCRATCHPAD),
        finish_reason=EnumProviderFinishReason.LENGTH,
    )

    assert not result.passed
    assert result.fail_category == "fail_deterministic"
    assert result.quality_score == 0.0
    assert len(result.failure_reasons) == 1
    assert "finish_reason=length" in result.failure_reasons[0]
    # It must CLIMB, not terminalise: the next rung carries its own budget.
    assert result.fallback_recommended
    # And the verdict must name the rule that decided it (OMN-18295).
    rules = {evaluation.rule: evaluation for evaluation in result.rule_evaluations}
    assert TRUNCATION_CHECK_NAME in rules
    assert not rules[TRUNCATION_CHECK_NAME].passed
    assert rules[TRUNCATION_CHECK_NAME].enforcement == "blocking"


def test_the_truncated_scratchpad_is_not_offered_as_the_answer() -> None:
    """The gate result must not read as though a finished answer was produced."""
    result = delta(
        _gate_input(_TRUNCATED_SCRATCHPAD),
        finish_reason=EnumProviderFinishReason.LENGTH,
    )
    assert result.actual_score is None
    assert result.pass_ is not True
    assert result.score_source == ""


def test_the_veto_does_not_depend_on_the_task_class() -> None:
    """A truncated response is refused whatever DoD the class declares.

    The textual truncation heuristics that do exist are bound to code-oriented
    criteria and never run for a prose class. The provider signal is not a
    content check and must not inherit that scoping.
    """
    for task_type, deterministic, heuristic in (
        ("document", _DOCUMENT_DETERMINISTIC, _DOCUMENT_HEURISTIC),
        ("code_generation", (), ()),
        ("research", (), ()),
    ):
        gate_input = ModelQualityGateInput(
            correlation_id=_CORRELATION_ID,
            task_type=task_type,
            llm_response_content=_TRUNCATED_SCRATCHPAD,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        )
        result = delta(gate_input, finish_reason=EnumProviderFinishReason.LENGTH)
        assert not result.passed, task_type
        assert result.fail_category == "fail_deterministic", task_type


def test_a_declared_response_contract_does_not_bypass_the_veto() -> None:
    """The schema branch replaces the DoD checks; it must not replace this one."""
    schema: dict[str, object] = {"type": "string"}
    result = delta(
        _gate_input(_TRUNCATED_SCRATCHPAD),
        response_contract=schema,
        finish_reason=EnumProviderFinishReason.LENGTH,
    )
    assert not result.passed
    assert result.fail_category == "fail_deterministic"


def test_the_refusal_names_truncation_rather_than_a_low_score() -> None:
    """The attempt record must say WHY, not just print a zero.

    The reason class ``deterministic_floor_failed`` was already honest, but the
    detail carried nothing but ``actual_score=0.000 required_bar=0.800``, which
    reads as a weak model. That is the misdirection OMN-18379 removed from the
    heuristic-veto branch; the floor branch short-circuits ahead of it and kept
    it until OMN-18278.
    """
    result = delta(
        _gate_input(_TRUNCATED_SCRATCHPAD),
        finish_reason=EnumProviderFinishReason.LENGTH,
    )
    decision, reason, detail = derive_attempt_acceptance(
        quality_passed=False,
        pre_filter_rejected=result.fail_category == "fail_deterministic",
        gate_passed=result.passed,
        judge_unavailable_floor=False,
        quality_score=result.quality_score,
        required_bar=0.8,
        rule_evaluations=result.rule_evaluations,
    )
    assert decision is EnumDelegationAcceptanceDecision.CLIMB
    assert reason is EnumDelegationAcceptanceReason.DETERMINISTIC_FLOOR_FAILED
    assert TRUNCATION_CHECK_NAME in detail
    assert "finish_reason=length" in detail


def test_a_floor_refusal_naming_no_rule_still_does_not_invent_one() -> None:
    """The detail gains a decider only when there IS one to point at."""
    _, _, detail = derive_attempt_acceptance(
        quality_passed=False,
        pre_filter_rejected=True,
        gate_passed=False,
        judge_unavailable_floor=False,
        quality_score=0.1,
        required_bar=0.8,
        rule_evaluations=(),
    )
    assert "vetoed_by" not in detail


# ---------------------------------------------------------------------------
# The truncated draft must not be handed back as the artifact
# ---------------------------------------------------------------------------


def test_a_truncated_last_attempt_is_not_the_terminal_artifact() -> None:
    """``_terminal_artifact`` is the ``or`` that surfaced the scratchpad.

    Guarding ``best_content`` alone would have been cosmetic: with one truncated
    attempt ``best_content`` is empty and control reaches this fallback.
    """
    truncated = ModelLlmDelegationCallResult(
        request_id="r1",
        success=True,
        content=_TRUNCATED_SCRATCHPAD,
        finish_reason=EnumProviderFinishReason.LENGTH,
    )
    complete = truncated.model_copy(
        update={
            "content": _COMPLETE_ANSWER,
            "finish_reason": EnumProviderFinishReason.STOP,
        }
    )

    assert _terminal_artifact("", truncated) == ""
    # An earlier rung's real draft still wins, unchanged (OMN-14220).
    assert _terminal_artifact(_COMPLETE_ANSWER, truncated) == _COMPLETE_ANSWER
    # And an untruncated last attempt is still the fallback it always was.
    assert _terminal_artifact("", complete) == _COMPLETE_ANSWER


# ---------------------------------------------------------------------------
# The positive control — a normal response is untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        EnumProviderFinishReason.STOP,
        EnumProviderFinishReason.ABSENT,
        EnumProviderFinishReason.UNRECOGNISED,
    ],
)
def test_a_complete_answer_still_passes(reason: EnumProviderFinishReason) -> None:
    """A real answer is accepted exactly as before, on every non-LENGTH signal."""
    result = delta(_gate_input(_COMPLETE_ANSWER), finish_reason=reason)
    assert result.passed
    assert result.failure_reasons == ()
    assert result.quality_score > 0.0


def test_the_veto_is_the_only_behaviour_change_for_a_complete_answer() -> None:
    """Supplying the signal must not perturb an otherwise-identical verdict."""
    without_signal = delta(_gate_input(_COMPLETE_ANSWER))
    with_signal = delta(
        _gate_input(_COMPLETE_ANSWER), finish_reason=EnumProviderFinishReason.STOP
    )
    assert without_signal.model_dump(
        exclude={"finish_reason"}
    ) == with_signal.model_dump(exclude={"finish_reason"})
