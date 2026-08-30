# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-16932 — a passing local answer ends the chain, and says why in typed terms.

Live evidence these tests are written against (dev lane, 2026-08-30, correlation
``cf245cad-dfa5-4fe5-bda3-f0a2972679ce``)::

    onex delegate 'Reply with exactly the word: alive' --task-type research \
        --bus kafka --kafka-bootstrap "$ONEX_DEV_LANE_BOOTSTRAP"

    attempt 0  local  qwen3.8  passed=false score=0.7
      TASK_MISMATCH: missing source citations or references
      TASK_MISMATCH: failed methodical_analysis
      WEAK_OUTPUT: response is a bare single-word fragment, fails semantic_adequacy
    attempt 1  local  qwen3.8  (identical)
    attempt 2  local  qwen3.8  (identical)
    attempt 3  cheap_cloud  gemini-2.5-flash   429 free-tier quota exceeded
    attempt 4  cheap_cloud  glm-5.3            429 insufficient balance
    terminal: failed

The model obeyed the instruction exactly. The gate scored the obedience against
``research``'s PROSE rubric — cite sources, reason methodically, do not be a
single word — rejected the free $0 answer three times, and climbed into two
metered 429s looking for a longer wrong answer.

Three separate claims are proven here, each RED before the fix:

1. the resolver reads the shape the PROMPT declared (contract-declared patterns);
2. the gate accepts the one-word answer under the declared shape override, and
   still rejects empty/truncated answers under it — it is an adequacy authority,
   not a rubber stamp;
3. every reason the gate emits is TRUE of the response it judged — the
   three-sentence+citation case must never carry a single-word-fragment reason.
"""

from __future__ import annotations

import uuid

import pytest

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.enums.enum_requested_response_shape import EnumRequestedResponseShape
from omnimarket.inference.requested_response_shape import (
    resolve_requested_response_shape,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_requested_shape_for_prompt,
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

# The exact prompt and the exact answer from the live run above.
LIVE_PROMPT = "Reply with exactly the word: alive"
LIVE_RESPONSE = "alive"

# A research answer that genuinely is three sentences with a citation. This is
# the case that must NEVER be described as a bare single-word fragment.
THREE_SENTENCE_CITED = (
    "Deterministic contracts reduce integration risk by fixing inputs, outputs "
    "and error behaviour that both sides can rely on. That predictability lets "
    "teams generate clients and tests early, because mismatches surface before "
    "deployment rather than at runtime. The OpenAPI Specification formalises "
    "this as a machine-readable contract (OpenAPI Initiative, 2017)."
)


def _gate(
    content: str,
    *,
    task_type: str,
    dod_deterministic: tuple[str, ...],
    dod_heuristic: tuple[str, ...],
) -> object:
    return delta(
        ModelQualityGateInput(
            correlation_id=str(uuid.uuid4()),
            task_type=task_type,
            llm_response_content=content,
            dod_deterministic=dod_deterministic,
            dod_heuristic=dod_heuristic,
        )
    )


# ---------------------------------------------------------------------------
# 1. The prompt's own declaration is read, from the contract, not from code.
# ---------------------------------------------------------------------------


def test_live_prompt_resolves_to_exact_literal_shape() -> None:
    """The prompt that produced the live failure declares EXACT_LITERAL."""
    assert (
        resolve_requested_shape_for_prompt(LIVE_PROMPT)
        is EnumRequestedResponseShape.EXACT_LITERAL
    )


def test_ordinary_research_prompt_stays_unconstrained() -> None:
    """A prompt that declares no shape must not acquire one.

    This is the regression guard on the whole feature: if this ever resolves to
    a constrained shape, every research request silently loses its prose rubric.
    """
    assert (
        resolve_requested_shape_for_prompt(
            "Explain in three sentences, with at least one cited source, why "
            "deterministic contracts reduce integration risk."
        )
        is EnumRequestedResponseShape.UNCONSTRAINED
    )


def test_absent_directives_never_constrain() -> None:
    """An empty directive mapping resolves UNCONSTRAINED for any prompt.

    A deployment whose contract predates the declaration keeps its exact prior
    behaviour — the feature cannot switch itself on.
    """
    assert (
        resolve_requested_response_shape(LIVE_PROMPT, {})
        is EnumRequestedResponseShape.UNCONSTRAINED
    )


def test_exact_literal_wins_over_single_word() -> None:
    """A prompt matching both directives resolves to the more specific one."""
    assert (
        resolve_requested_shape_for_prompt(
            "Answer in one word. Reply with exactly the word: alive"
        )
        is EnumRequestedResponseShape.EXACT_LITERAL
    )


# ---------------------------------------------------------------------------
# 2. The declared shape selects a DoD set the obedient answer can satisfy.
# ---------------------------------------------------------------------------


def test_research_dod_is_unchanged_without_a_prompt() -> None:
    """The prompt-less signature keeps the class rubric byte-for-byte."""
    det, heur = resolve_task_class_dod_checks("research")
    assert det == ("response_non_empty",)
    assert heur == (
        "no_refusal",
        "cites_sources",
        "methodical_analysis",
        "semantic_adequacy",
    )


def test_research_dod_drops_the_prose_rubric_for_a_declared_literal() -> None:
    """The live prompt selects the contract's shape_overrides heuristic band.

    RED before the fix: ``resolve_task_class_dod_checks`` took no prompt at all,
    so the prose rubric was applied to every request in the class.
    """
    det, heur = resolve_task_class_dod_checks("research", prompt=LIVE_PROMPT)
    # The deterministic floor is NEVER lowered by a shape directive.
    assert det == ("response_non_empty",)
    assert "cites_sources" not in heur
    assert "methodical_analysis" not in heur
    assert heur == ("no_refusal", "short_form_adequacy")


def test_live_one_word_answer_now_passes_the_gate() -> None:
    """The exact live response, under the exact live prompt, is ACCEPTED.

    This is the ticket's goal: a passing local answer ends the chain. RED before
    the fix — the same input scored 0.7 and failed.
    """
    det, heur = resolve_task_class_dod_checks("research", prompt=LIVE_PROMPT)
    result = _gate(
        LIVE_RESPONSE, task_type="research", dod_deterministic=det, dod_heuristic=heur
    )
    assert result.passed is True, result.failure_reasons
    assert result.failure_reasons == ()
    assert result.quality_score == 1.0
    assert result.fallback_recommended is False


def test_live_one_word_answer_failed_before_the_shape_override() -> None:
    """The unshaped rubric is what rejected it — the defect, pinned.

    Guards against a fix that quietly weakened ``semantic_adequacy`` for every
    request instead of scoping the change to a declared shape.
    """
    result = _gate(
        LIVE_RESPONSE,
        task_type="research",
        dod_deterministic=("response_non_empty",),
        dod_heuristic=(
            "no_refusal",
            "cites_sources",
            "methodical_analysis",
            "semantic_adequacy",
        ),
    )
    assert result.passed is False
    assert result.quality_score == 0.7


@pytest.mark.parametrize(
    ("content", "expected_fragment"),
    [
        ("", "response is empty"),
        ("   ", "response is empty"),
        ("The answer is (", "truncated mid-token"),
        ("The change adds a graded score so the", "truncated mid-clause"),
    ],
)
def test_short_form_adequacy_still_rejects_real_inadequacy(
    content: str, expected_fragment: str
) -> None:
    """The shape override is an adequacy authority, not a rubber stamp."""
    det, heur = resolve_task_class_dod_checks("research", prompt=LIVE_PROMPT)
    result = _gate(
        content, task_type="research", dod_deterministic=det, dod_heuristic=heur
    )
    assert result.passed is False
    assert any(expected_fragment in reason for reason in result.failure_reasons), (
        result.failure_reasons
    )


def test_short_form_adequacy_rejects_a_refusal() -> None:
    """A declared shape never licenses a refusal."""
    det, heur = resolve_task_class_dod_checks("research", prompt=LIVE_PROMPT)
    result = _gate(
        "I cannot help with that.",
        task_type="research",
        dod_deterministic=det,
        dod_heuristic=heur,
    )
    assert result.passed is False
    assert any(r.startswith("REFUSAL") for r in result.failure_reasons)


# ---------------------------------------------------------------------------
# 3. Every emitted reason is TRUE of the response it judged.
# ---------------------------------------------------------------------------

_SINGLE_WORD_REASON_FRAGMENT = "bare single-word fragment"


def test_three_sentence_cited_answer_never_gets_a_single_word_reason() -> None:
    """A three-sentence, cited answer must not be called a single-word fragment.

    The reason string is the operator-facing explanation of why a free rung was
    abandoned. A reason that is false of the response it judged makes the
    escalation unauditable — which is how the live defect stayed invisible.
    """
    for heuristic in (
        (
            "no_refusal",
            "cites_sources",
            "methodical_analysis",
            "semantic_adequacy",
        ),
        ("no_refusal", "short_form_adequacy"),
    ):
        result = _gate(
            THREE_SENTENCE_CITED,
            task_type="research",
            dod_deterministic=("response_non_empty",),
            dod_heuristic=heuristic,
        )
        assert not any(
            _SINGLE_WORD_REASON_FRAGMENT in reason for reason in result.failure_reasons
        ), (heuristic, result.failure_reasons)


@pytest.mark.parametrize(
    "cited_answer",
    [
        # An organisation as author — the exact form in THREE_SENTENCE_CITED.
        "Contracts fix inputs and outputs (OpenAPI Initiative, 2017).",
        # Three-token organisational author.
        "Airborne spread is established (World Health Organization, 2021).",
        # The check's OWN docstring names this form as one it matches.
        "Contract tests catch drift before deployment (Smith et al., 2020).",
    ],
)
def test_cites_sources_reason_is_never_false_of_a_cited_answer(
    cited_answer: str,
) -> None:
    """A response that visibly cites a source is never told it has none.

    RED before this fix. ``cites_sources`` recognised an author-year citation
    only when the author was a SINGLE capitalised token, so every one of these
    answers — each carrying a plain, human-legible ``(Author, Year)`` — was
    judged ``TASK_MISMATCH: missing source citations or references``. The
    ``et al.`` case is the sharpest: the check's own docstring advertises
    ``(Smith et al., 2020)`` as a form it matches, and it did not.

    This is the same class of defect as the single-word verdict this ticket was
    filed for, and the more damaging half of it. A reason that is false of the
    response it judged does not merely mislead a reader — it sends a correct,
    free, local answer up a metered ladder to buy a longer answer that would
    have been rejected for the same untrue cause. An escalation is only
    auditable if the stated cause is true of what was actually judged.
    """
    result = _gate(
        cited_answer,
        task_type="research",
        dod_deterministic=("response_non_empty",),
        dod_heuristic=("no_refusal", "cites_sources", "semantic_adequacy"),
    )
    assert not any(
        "missing source citations" in reason for reason in result.failure_reasons
    ), result.failure_reasons


def test_cites_sources_still_rejects_a_genuinely_unattributed_answer() -> None:
    """The citation check remains a real authority, not a rubber stamp.

    Guards the fix above against the lazy version of itself: widening the
    author-year form must not make every parenthesis look like a citation.
    """
    result = _gate(
        "Contracts reduce risk because both sides agree on the shape of the "
        "payload (in practice, teams learn this the hard way).",
        task_type="research",
        dod_deterministic=("response_non_empty",),
        dod_heuristic=("no_refusal", "cites_sources", "semantic_adequacy"),
    )
    assert any(
        "missing source citations" in reason for reason in result.failure_reasons
    ), result.failure_reasons


def test_every_short_form_reason_names_the_check_that_produced_it() -> None:
    """A reason emitted by ``short_form_adequacy`` says so.

    Before OMN-16932 the only single-word verdict available was
    ``semantic_adequacy``'s, so a reader could not tell which rule fired.
    """
    det, heur = resolve_task_class_dod_checks("research", prompt=LIVE_PROMPT)
    result = _gate(
        "The answer is (",
        task_type="research",
        dod_deterministic=det,
        dod_heuristic=heur,
    )
    weak = [r for r in result.failure_reasons if r.startswith("WEAK_OUTPUT")]
    assert weak
    assert all("short_form_adequacy" in r for r in weak), weak


def test_single_word_reason_is_true_when_it_is_emitted() -> None:
    """The unshaped rubric's single-word reason is true of a single word.

    The reason is not wrong in general — it was wrong for a request that ASKED
    for one word. This pins the honest case so the fix cannot be read as
    deleting a true diagnostic.
    """
    result = _gate(
        LIVE_RESPONSE,
        task_type="research",
        dod_deterministic=("response_non_empty",),
        dod_heuristic=("no_refusal", "semantic_adequacy"),
    )
    reasons = [r for r in result.failure_reasons if _SINGLE_WORD_REASON_FRAGMENT in r]
    assert reasons
    assert len(LIVE_RESPONSE.split()) == 1


# ---------------------------------------------------------------------------
# 4. The accept/climb decision is TYPED and recorded, not inferred from prose.
# ---------------------------------------------------------------------------


class TestTypedAcceptanceDecision:
    """``_acceptance_decision`` derives the verdict from the acceptance expression.

    The orchestrator has always evaluated ``quality_accepted``; it has never
    recorded it. Every branch is pinned here so the recorded reason cannot drift
    from the branch actually taken.
    """

    def test_accepted_on_the_bar(self) -> None:
        assert HandlerDelegationWorkflow._acceptance_decision(
            pre_filter_rejected=False,
            gate_passed=True,
            judge_unavailable_floor=False,
            score_below_required_bar=False,
        ) == (
            EnumDelegationAcceptanceDecision.ACCEPT,
            EnumDelegationAcceptanceReason.QUALITY_BAR_MET,
        )

    def test_accepted_on_the_deterministic_floor_when_the_judge_is_gone(self) -> None:
        """OMN-13959's degraded acceptance is an ACCEPT with its own reason.

        It is not the same event as clearing the bar and must not be recorded as
        if it were — a cloud-judge outage is exactly the state in which a reader
        needs to know which authority accepted the answer.
        """
        assert HandlerDelegationWorkflow._acceptance_decision(
            pre_filter_rejected=False,
            gate_passed=True,
            judge_unavailable_floor=True,
            score_below_required_bar=True,
        ) == (
            EnumDelegationAcceptanceDecision.ACCEPT,
            EnumDelegationAcceptanceReason.JUDGE_UNAVAILABLE_DETERMINISTIC_FLOOR,
        )

    def test_deterministic_floor_short_circuits(self) -> None:
        """A hard-floor failure is reported as itself, not as a low score."""
        assert HandlerDelegationWorkflow._acceptance_decision(
            pre_filter_rejected=True,
            gate_passed=False,
            judge_unavailable_floor=False,
            score_below_required_bar=False,
        ) == (
            EnumDelegationAcceptanceDecision.CLIMB,
            EnumDelegationAcceptanceReason.DETERMINISTIC_FLOOR_FAILED,
        )

    def test_criteria_failure_above_the_bar_is_not_called_a_low_score(self) -> None:
        """The OMN-15464 0.867-vs-0.800 lie, now impossible in the typed reason."""
        assert HandlerDelegationWorkflow._acceptance_decision(
            pre_filter_rejected=False,
            gate_passed=False,
            judge_unavailable_floor=False,
            score_below_required_bar=False,
        ) == (
            EnumDelegationAcceptanceDecision.CLIMB,
            EnumDelegationAcceptanceReason.ACCEPTANCE_CRITERIA_FAILED,
        )

    def test_sub_bar_score_is_reported_as_sub_bar(self) -> None:
        assert HandlerDelegationWorkflow._acceptance_decision(
            pre_filter_rejected=False,
            gate_passed=True,
            judge_unavailable_floor=False,
            score_below_required_bar=True,
        ) == (
            EnumDelegationAcceptanceDecision.CLIMB,
            EnumDelegationAcceptanceReason.SCORE_BELOW_REQUIRED_BAR,
        )

    def test_the_decision_agrees_with_the_acceptance_expression(self) -> None:
        """Exhaustive cross-check against ``quality_accepted`` itself.

        The typed decision is a second reading of the same four booleans. If the
        two ever disagree the event log is lying about an outcome the pipeline
        actually took, so every combination is checked rather than sampled.
        """
        for pre_filter in (True, False):
            for passed in (True, False):
                for judge_floor in (True, False):
                    for below_bar in (True, False):
                        expected_accept = (
                            not pre_filter and passed and (judge_floor or not below_bar)
                        )
                        decision, _reason = (
                            HandlerDelegationWorkflow._acceptance_decision(
                                pre_filter_rejected=pre_filter,
                                gate_passed=passed,
                                judge_unavailable_floor=judge_floor,
                                score_below_required_bar=below_bar,
                            )
                        )
                        assert (
                            decision is EnumDelegationAcceptanceDecision.ACCEPT
                        ) is expected_accept, (
                            pre_filter,
                            passed,
                            judge_floor,
                            below_bar,
                        )


# ---------------------------------------------------------------------------
# 4. The operator's OTHER live prompt — the determiner must not decide the case.
# ---------------------------------------------------------------------------

# Row 1 of the 2026-08-30 beta feature rollup, in-memory bus, 14:42:43Z. Same
# defect, same ladder, different determiner ("the single word" vs "exactly the
# word"). It is recorded separately because a directive set that matched only
# the 14:47Z phrasing would have left the operator's own first probe still
# climbing the paid ladder while this ticket read as fixed.
ROLLUP_ROW1_PROMPT = "reply with the single word: ok"


def test_rollup_row1_prompt_is_also_recognised_as_constrained() -> None:
    """ "reply with the single word: ok" declares a shape.

    RED before the determiner was opened: the directive alternation accepted
    only ``(?:a )?single word``, so "the single word" resolved UNCONSTRAINED and
    this prompt kept the full ``research`` prose rubric.
    """
    assert (
        resolve_requested_shape_for_prompt(ROLLUP_ROW1_PROMPT)
        is EnumRequestedResponseShape.SINGLE_WORD
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "reply with the single word: ok",
        "Respond with only one word",
        "answer with a single word",
        "Explain in one word why this matters",
    ],
)
def test_single_word_phrasings_all_resolve_constrained(prompt: str) -> None:
    """Ordinary ways of asking for one word all reach the same declared shape."""
    assert (
        resolve_requested_shape_for_prompt(prompt)
        is not EnumRequestedResponseShape.UNCONSTRAINED
    )


def test_rollup_row1_prompt_gets_the_short_form_band() -> None:
    """The row-1 prompt selects the short-form adequacy authority, not the rubric."""
    det, heur = resolve_task_class_dod_checks("research", prompt=ROLLUP_ROW1_PROMPT)
    assert det == ("response_non_empty",)
    assert heur == ("no_refusal", "short_form_adequacy")


def test_broadened_directives_do_not_capture_ordinary_prose() -> None:
    """Opening the determiner must not make ordinary research prose constrained.

    The guard on the guard: these read ABOUT one-word answers rather than asking
    for one, and must keep the class rubric.
    """
    for prompt in (
        "Discuss why one word is rarely enough in technical documentation.",
        "Summarise the routing architecture and cite your sources.",
    ):
        assert (
            resolve_requested_shape_for_prompt(prompt)
            is EnumRequestedResponseShape.UNCONSTRAINED
        )


# ---------------------------------------------------------------------------
# 5. The winning rung is legible on the CONSUMER-facing terminal.
# ---------------------------------------------------------------------------


def test_accepted_rung_is_not_reported_as_a_failed_attempt() -> None:
    """An ACCEPT row in escalation history renders as a passing attempt.

    RED once the accepted attempt began being recorded in ``escalation_history``:
    the terminal projection hardcoded ``quality_gate_passed=False`` for every
    history row, because history had only ever held rejections. That would have
    relabelled the rung that ANSWERED as a failure — the precise illegibility
    this ticket exists to remove.
    """
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
        _attempt_records,
    )

    records = _attempt_records(
        {
            "escalation_history": [
                {
                    "tier_name": "local",
                    "model_used": "qwen3.8",
                    "quality_score": 1.0,
                    "failure_reasons": [],
                    "cost_usd": 0.0,
                    "acceptance_decision": "accept",
                    "acceptance_reason": "quality_bar_met",
                }
            ]
        }
    )

    assert len(records) == 1
    accepted = records[0]
    assert accepted.quality_gate_passed is True
    assert accepted.acceptance_decision is EnumDelegationAcceptanceDecision.ACCEPT
    assert accepted.acceptance_reason is EnumDelegationAcceptanceReason.QUALITY_BAR_MET


def test_climbed_rung_carries_its_typed_reason_on_the_terminal() -> None:
    """A rejected rung reports CLIMB plus the typed reason it was abandoned for."""
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
        _attempt_records,
    )

    records = _attempt_records(
        {
            "escalation_history": [
                {
                    "tier_name": "local",
                    "model_used": "qwen3.8",
                    "quality_score": 0.7,
                    "failure_reasons": ["WEAK_OUTPUT: ..."],
                    "cost_usd": 0.0,
                    "acceptance_decision": "climb",
                    "acceptance_reason": "score_below_required_bar",
                }
            ]
        }
    )

    climbed = records[0]
    assert climbed.quality_gate_passed is False
    assert climbed.acceptance_decision is EnumDelegationAcceptanceDecision.CLIMB
    assert (
        climbed.acceptance_reason
        is EnumDelegationAcceptanceReason.SCORE_BELOW_REQUIRED_BAR
    )


def test_history_row_without_a_decision_keeps_the_rejected_reading() -> None:
    """A record predating the typed field still reads as a rejection, not an accept."""
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
        _attempt_records,
    )

    records = _attempt_records(
        {
            "escalation_history": [
                {
                    "tier_name": "local",
                    "model_used": "qwen3.8",
                    "quality_score": 0.7,
                    "failure_reasons": ["WEAK_OUTPUT: ..."],
                    "cost_usd": 0.0,
                }
            ]
        }
    )

    assert records[0].quality_gate_passed is False
    assert records[0].acceptance_decision is None
    assert records[0].acceptance_reason is None
