# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18379: a leaked reasoning preamble must not veto, and must not mislabel.

Three defects, one delegation run. The run is real: `43d269f5-d2ed-44cc-9338-
04fdd2034d9a`, `document` class, 2026-09-14, recorded verbatim under
``tests/fixtures/delegation/omn18379/``.

1. The local model answered correctly and prefixed its answer with a plain-text
   reasoning scratchpad ("Here's a thinking process:"), terminated by a closing
   trace tag with no matching opener. The paired-tag strip cannot remove it.
2. The blocking heuristic ``accurate`` scans the WHOLE response for hedging
   phrases and fired on the word "unverified", which occurs only inside that
   scratchpad. The answer itself hedges nothing.
3. The refusal was recorded as ``score_below_required_bar`` at score 0.900
   against the ``document`` class bar of 0.800 -- a label that contradicts its
   own printed numbers, exactly the class of lie OMN-15464 removed from the bus
   path and never removed from the bus-less local path.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.delegation.reasoning_preamble import (
    EnumReasoningBoundaryRule,
    segment_reasoning_preamble,
)
from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.inference.task_class_authority import (
    resolve_reasoning_preamble_policy,
)
from omnimarket.models.delegation.wire.model_quality_gate import (
    ModelQualityRuleEvaluation,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    derive_attempt_acceptance,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "delegation" / "omn18379"

# The `document` class DoD, verbatim from configs/task_class_contracts.v1.yaml.
# `identifiers_grounded` is omitted: it is evaluated only when the caller supplies
# a grounding source, which these fixtures do not carry.
_DOCUMENT_HEURISTIC = ("no_refusal", "accurate", "semantic_adequacy")
_DOCUMENT_DETERMINISTIC = ("response_non_empty",)
_DOCUMENT_REQUIRED_BAR = 0.8

# The refused run's own correlation id, so a failure points at real evidence.
_CORRELATION_ID = UUID("43d269f5-d2ed-44cc-9338-04fdd2034d9a")


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _gate_input(content: str) -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=_CORRELATION_ID,
        task_type="document",
        llm_response_content=content,
        dod_deterministic=_DOCUMENT_DETERMINISTIC,
        dod_heuristic=_DOCUMENT_HEURISTIC,
    )


# ---------------------------------------------------------------------------
# AC2 -- the deterministic segmenter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture_name", "answer_prefix"),
    [
        # The refused run: a JSON answer behind 122 lines of scratchpad.
        ("43d269f5_leaked_preamble_unpaired_think.txt", "{"),
        # One of the two runs that broke the hourly renderer.
        ("fd67cd3c_leaked_preamble_markdown_answer.txt", "# Collection Window:"),
        # A plain-prose answer: no header, no fence, only the terminator.
        ("3d4dd739_leaked_preamble_plain_answer.txt", "GOAL:"),
    ],
)
def test_ac2_unpaired_closing_tag_is_the_boundary(
    fixture_name: str, answer_prefix: str
) -> None:
    """Every leak in the live corpus closes with an opener-less trace tag."""
    raw = _fixture(fixture_name)
    assert "<think>" not in raw, "fixture must carry an UNPAIRED terminator"
    segmentation = segment_reasoning_preamble(raw)

    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.UNPAIRED_CLOSING_TAG
    assert segmentation.answer.startswith(answer_prefix)
    assert "Here's a thinking process:" in segmentation.preamble
    # The offset indexes the ORIGINAL response, so an auditor can point at the
    # seam instead of trusting a copy of the two halves.
    assert raw[segmentation.boundary_offset :] == segmentation.answer
    assert raw.startswith(segmentation.preamble)


def test_ac2_markdown_header_is_the_boundary_when_no_tag_is_emitted() -> None:
    """Derived fixture: the same leak with its terminator line deleted."""
    raw = _fixture("fd67cd3c_derived_no_trace_tag.txt")
    assert "think>" not in raw
    segmentation = segment_reasoning_preamble(raw)

    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.MARKDOWN_HEADER
    assert segmentation.answer.startswith("# Collection Window:")
    assert "Here's a thinking process:" in segmentation.preamble


def test_ac2_fenced_block_is_the_boundary_when_the_answer_is_a_fence() -> None:
    """Synthetic: an answer that opens with a fence and no header."""
    raw = "Here's a thinking process:\n\n1. Consider the ask.\n\n```json\n{}\n```\n"
    segmentation = segment_reasoning_preamble(raw)

    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.FENCED_BLOCK
    assert segmentation.answer.startswith("```json")


def test_ac2_no_boundary_verifies_the_whole_response_and_says_so() -> None:
    """Rule 8: a response with no resolvable boundary is never silently cut.

    The three assertions carrying that invariant — whole answer, empty
    preamble, zero offset — are unchanged and are the point of this test.

    The RULE LABEL moved under OMN-18967 AC3 and the invariant did not. This
    fixture opens with `Here's a thinking process:`, a declared lead-in, and
    no boundary resolves behind it, which is exactly the case AC3 separated
    out. It now reports `preamble_unresolved` rather than
    `no_boundary_found`, because the two were previously the same value and a
    consumer could not tell "there was nothing to strip" from "the whole
    response is scratchpad". Nothing about the cutting behaviour changed; only
    the report of why nothing was cut did.

    The clean case, a response with neither a lead-in nor a boundary, still
    reports `no_boundary_found`, and the sibling module
    `test_unresolved_preamble_typed_refusal_omn18967.py` pins both directions.
    """
    raw = "Here's a thinking process:\n\n1. Consider the ask.\n2. Answer it.\n"
    segmentation = segment_reasoning_preamble(raw)

    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.PREAMBLE_UNRESOLVED
    assert segmentation.answer == raw
    assert segmentation.preamble == ""
    assert segmentation.boundary_offset == 0


def test_ac2_explicit_answer_marker_is_the_boundary() -> None:
    """A prompt-requested answer marker outranks the structural boundaries."""
    policy = resolve_reasoning_preamble_policy()
    assert policy is not None, "the contract must declare a reasoning_preamble policy"
    marker = policy.answer_markers[0]
    raw = (
        f"Here's a thinking process:\n\n1. Consider the ask.\n\n"
        f"{marker}\n\n# Title\n\nBody.\n"
    )

    segmentation = segment_reasoning_preamble(raw)

    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.ANSWER_MARKER
    assert segmentation.answer.startswith("# Title")
    assert marker in segmentation.preamble


def test_ac2_a_response_with_no_lead_in_is_never_segmented() -> None:
    """A structural boundary alone strips nothing: the prose is the answer."""
    raw = (
        "This report covers the delivery window and what changed in it.\n\n"
        "# Findings\n\nOne finding, stated plainly.\n"
    )
    segmentation = segment_reasoning_preamble(raw)

    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.NO_BOUNDARY_FOUND
    assert segmentation.answer == raw
    assert segmentation.preamble == ""


def test_ac2_segmenter_is_idempotent() -> None:
    """Segmenting an answer segment again is a no-op, so double application is safe."""
    raw = _fixture("43d269f5_leaked_preamble_unpaired_think.txt")
    once = segment_reasoning_preamble(raw)
    twice = segment_reasoning_preamble(once.answer)

    assert twice.answer == once.answer
    assert twice.boundary_rule is EnumReasoningBoundaryRule.NO_BOUNDARY_FOUND


def test_ac2_policy_is_declared_in_the_contract_not_in_python() -> None:
    """The lead-ins, markers and tags are contract-declared, never guessed."""
    policy = resolve_reasoning_preamble_policy()
    assert policy is not None

    assert policy.lead_in_phrases
    assert policy.answer_markers
    assert policy.closing_trace_tags
    assert any(
        "thinking process" in phrase.lower() for phrase in policy.lead_in_phrases
    ), "the observed leak's own lead-in must be declared"


# ---------------------------------------------------------------------------
# AC3 -- phrase heuristics evaluate the answer segment only
# ---------------------------------------------------------------------------


def test_ac3_accurate_does_not_fire_on_a_hedge_that_is_only_in_the_preamble() -> None:
    """The whole defect, end to end, through the real gate entry point."""
    raw = _fixture("43d269f5_leaked_preamble_unpaired_think.txt")
    assert "unverified" in raw.lower(), "fixture must still carry the hedging phrase"
    segmentation = segment_reasoning_preamble(raw)
    assert "unverified" not in segmentation.answer.lower(), (
        "positive control: the hedge is in the preamble, not the answer"
    )

    result = delta(_gate_input(raw))

    accurate = next(
        evaluation
        for evaluation in result.rule_evaluations
        if evaluation.rule == "accurate"
    )
    assert accurate.passed, accurate.detail
    assert result.passed


def test_ac3_accurate_still_fires_when_the_answer_itself_hedges() -> None:
    """Positive control: the veto is intact for a real self-disclaimer."""
    raw = (
        "Here's a thinking process:\n\n1. Draft it.\n\n</think>\n\n"
        "# Report\n\nThis summary may be inaccurate and should not be relied on.\n"
    )
    result = delta(_gate_input(raw))

    accurate = next(
        evaluation
        for evaluation in result.rule_evaluations
        if evaluation.rule == "accurate"
    )
    assert not accurate.passed
    assert not result.passed


@pytest.mark.parametrize(
    "fixture_name",
    [
        "43d269f5_leaked_preamble_unpaired_think.txt",
        "fd67cd3c_leaked_preamble_markdown_answer.txt",
        "3d4dd739_leaked_preamble_plain_answer.txt",
    ],
)
def test_ac2_what_the_gate_judged_is_what_the_caller_receives(
    fixture_name: str,
) -> None:
    """The dispatch port strips for `result.txt` with the same pure function.

    Both seams call ``segment_reasoning_preamble``, so the text the gate judged
    and the text written to ``result.txt`` cannot diverge. This pins that, by
    reconstructing the caller's text from what the gate recorded it removed.
    """
    raw = _fixture(fixture_name)
    result = delta(_gate_input(raw))
    caller_text = segment_reasoning_preamble(raw).answer

    assert not caller_text.startswith("Here's a thinking process:")
    assert raw.removeprefix(result.reasoning_preamble).lstrip() == caller_text


def test_ac3_the_stripped_preamble_is_reported_for_audit() -> None:
    """Never silently dropped: the gate result carries what it removed."""
    raw = _fixture("43d269f5_leaked_preamble_unpaired_think.txt")
    result = delta(_gate_input(raw))

    assert result.reasoning_preamble_rule == (
        EnumReasoningBoundaryRule.UNPAIRED_CLOSING_TAG.value
    )
    assert "Here's a thinking process:" in (result.reasoning_preamble or "")


# ---------------------------------------------------------------------------
# AC1 -- the receipt names the deciding check
# ---------------------------------------------------------------------------


def test_ac1_recorded_receipt_label_contradicts_its_own_numbers() -> None:
    """The defect, read off the real receipt: 0.900 reported as sub-0.800."""
    receipt = json.loads(_fixture("43d269f5_refused_receipt.json"))
    refused = [
        attempt
        for attempt in receipt["attempts"]
        # The fourth attempt is a provider 503 and carries no score at all; the
        # three graded local rungs are the ones this ticket is about.
        if attempt["acceptance_decision"]
        == EnumDelegationAcceptanceDecision.CLIMB.value
        and attempt["quality_score"] is not None
    ]
    assert len(refused) == 3, "fixture must carry the three graded local refusals"
    for attempt in refused:
        assert attempt["quality_score"] >= _DOCUMENT_REQUIRED_BAR
        assert (
            attempt["acceptance_reason"]
            == EnumDelegationAcceptanceReason.SCORE_BELOW_REQUIRED_BAR.value
        ), "this fixture records the pre-fix mislabel"


def test_ac1_a_vetoing_heuristic_is_never_reported_as_a_bar_miss() -> None:
    """Drive a 0.9 score with a vetoing blocking rule; the label must name it."""
    decision, reason, detail = derive_attempt_acceptance(
        quality_passed=False,
        pre_filter_rejected=False,
        gate_passed=False,
        judge_unavailable_floor=False,
        quality_score=0.9,
        required_bar=_DOCUMENT_REQUIRED_BAR,
        rule_evaluations=(
            ModelQualityRuleEvaluation(
                rule="accurate",
                enforcement="blocking",
                passed=False,
                threshold=None,
                threshold_unit=None,
                detail=(
                    "TASK_MISMATCH: response explicitly disclaims accuracy: "
                    "unverified@offset=1421"
                ),
            ),
        ),
    )

    assert decision is EnumDelegationAcceptanceDecision.CLIMB
    assert reason is not EnumDelegationAcceptanceReason.SCORE_BELOW_REQUIRED_BAR
    assert reason is EnumDelegationAcceptanceReason.HEURISTIC_VETO
    assert "accurate" in detail
    assert "unverified" in detail
    assert "offset=" in detail


def test_ac1_a_real_bar_miss_is_still_a_bar_miss() -> None:
    """Positive control: the bar-miss label survives for an actual bar miss."""
    decision, reason, detail = derive_attempt_acceptance(
        quality_passed=False,
        pre_filter_rejected=False,
        gate_passed=True,
        judge_unavailable_floor=False,
        quality_score=0.5,
        required_bar=_DOCUMENT_REQUIRED_BAR,
        rule_evaluations=(),
    )

    assert decision is EnumDelegationAcceptanceDecision.CLIMB
    assert reason is EnumDelegationAcceptanceReason.SCORE_BELOW_REQUIRED_BAR
    assert "0.500" in detail
    assert "0.800" in detail


def test_ac1_an_accepted_attempt_reports_the_bar_met() -> None:
    """Positive control: acceptance is unchanged."""
    decision, reason, _ = derive_attempt_acceptance(
        quality_passed=True,
        pre_filter_rejected=False,
        gate_passed=True,
        judge_unavailable_floor=False,
        quality_score=1.0,
        required_bar=_DOCUMENT_REQUIRED_BAR,
        rule_evaluations=(),
    )

    assert decision is EnumDelegationAcceptanceDecision.ACCEPT
    assert reason is EnumDelegationAcceptanceReason.QUALITY_BAR_MET


def test_ac1_a_deterministic_floor_failure_outranks_everything() -> None:
    """Precedence matches the acceptance expression the bus path evaluates."""
    _, reason, _ = derive_attempt_acceptance(
        quality_passed=False,
        pre_filter_rejected=True,
        gate_passed=False,
        judge_unavailable_floor=False,
        quality_score=0.1,
        required_bar=_DOCUMENT_REQUIRED_BAR,
        rule_evaluations=(),
    )

    assert reason is EnumDelegationAcceptanceReason.DETERMINISTIC_FLOOR_FAILED


def test_ac1_gate_rejection_with_no_blocking_rule_named_is_criteria_failed() -> None:
    """A rejection the rule evaluations cannot explain is not relabelled a veto."""
    _, reason, _ = derive_attempt_acceptance(
        quality_passed=False,
        pre_filter_rejected=False,
        gate_passed=False,
        judge_unavailable_floor=False,
        quality_score=0.9,
        required_bar=_DOCUMENT_REQUIRED_BAR,
        rule_evaluations=(),
    )

    assert reason is EnumDelegationAcceptanceReason.ACCEPTANCE_CRITERIA_FAILED
