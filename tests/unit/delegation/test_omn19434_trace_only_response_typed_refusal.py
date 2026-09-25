# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19434: reasoning with no answer behind it is refused as such, not as "empty response".

OMN-18967 gave one shape its own typed refusal: a response that OPENS with a
declared reasoning lead-in phrase and has no declared boundary behind it. Two
shapes with the same meaning were left out, because they carry no lead-in
phrase:

* a paired reasoning block and nothing after it. The segmenter left it whole
  (a paired block is "owned by the ordinary paired-tag strip"), the gate's
  paired-tag strip removed the whole response, and the gate reported
  ``MALFORMED: empty response`` about a response that was hundreds of tokens
  long;
* reasoning ending in the model's own unpaired trace terminator, with nothing
  after it. The segmenter left it whole, and the gate graded the scratchpad as
  though it were the answer.

A caller reading "empty response" goes looking for a provider fault that did
not happen (OMN-19224, comment c0dbcdd2). Both shapes now resolve to
``preamble_unresolved``, which carries the typed refusal and names the rule,
and a genuinely empty response still reads as empty.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.delegation.reasoning_preamble import (
    UNRESOLVED_PREAMBLE_CHECK_NAME,
    EnumReasoningBoundaryRule,
    output_refusal_for_segmentation,
    segment_reasoning_preamble,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models import (
    ModelQualityGateInput,
)

pytestmark = pytest.mark.unit

_CORRELATION_ID = UUID("dd548066-0cc5-4b34-857e-693fb531ab82")

_DOCUMENT_DETERMINISTIC = ("response_non_empty",)
_DOCUMENT_HEURISTIC = ("no_refusal", "accurate", "semantic_adequacy")

_EMPTY_REASON_FRAGMENT = "empty"

#: Reasoning in a paired block, and nothing after it.
_PAIRED_TRACE_ONLY = (
    "<think>\n"
    "The user asked me to reply with one word. The word they want is OK. I "
    "should not add anything else, so the reply is just that word.\n"
    "</think>\n\n"
)

#: Reasoning closed by the model's own terminator, with no opener and no
#: declared lead-in phrase, and nothing after it.
_UNPAIRED_TRACE_ONLY = (
    "The request is for a single word. The word they want is OK, and nothing "
    "else should follow it.\n"
    "</think>\n"
)

#: The same two shapes with the answer present, which must still pass.
_PAIRED_TRACE_THEN_ANSWER = (
    _PAIRED_TRACE_ONLY + "OK, the build is green and ready to ship."
)
_UNPAIRED_TRACE_THEN_ANSWER = (
    _UNPAIRED_TRACE_ONLY + "OK, the build is green and ready to ship."
)


def _gate(content: str) -> tuple[tuple[str, ...], set[str]]:
    result = delta(
        ModelQualityGateInput(
            correlation_id=_CORRELATION_ID,
            task_type="document",
            llm_response_content=content,
            dod_deterministic=_DOCUMENT_DETERMINISTIC,
            dod_heuristic=_DOCUMENT_HEURISTIC,
        )
    )
    assert not result.passed or not result.failure_reasons
    failed_rules = {
        evaluation.rule
        for evaluation in result.rule_evaluations
        if not evaluation.passed
    }
    return result.failure_reasons, failed_rules


# ---------------------------------------------------------------------------
# AC1: a trace-only response is refused naming the preamble rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [_PAIRED_TRACE_ONLY, _UNPAIRED_TRACE_ONLY],
    ids=["paired-trace-only", "unpaired-trace-only"],
)
def test_the_segmenter_reports_no_answer_behind_the_trace(content: str) -> None:
    """The segmenter names the outcome instead of calling the response clean."""
    segmentation = segment_reasoning_preamble(content)
    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.PREAMBLE_UNRESOLVED
    assert output_refusal_for_segmentation(segmentation) is not None
    # Nothing is dropped on a verdict: the text is carried for audit.
    assert segmentation.answer == content


@pytest.mark.parametrize(
    "content",
    [_PAIRED_TRACE_ONLY, _UNPAIRED_TRACE_ONLY],
    ids=["paired-trace-only", "unpaired-trace-only"],
)
def test_the_gate_refuses_it_naming_the_preamble_rule_not_empty(content: str) -> None:
    """AC1: the reason names the preamble rule; it does not say empty."""
    reasons, failed_rules = _gate(content)
    assert reasons, "a trace-only response passed the gate"
    assert failed_rules == {UNRESOLVED_PREAMBLE_CHECK_NAME}, failed_rules
    for reason in reasons:
        assert "response is empty" not in reason, reason
        assert "empty response" not in reason, reason
    assert any("no declared boundary resolved" in reason for reason in reasons)


# ---------------------------------------------------------------------------
# AC2: a genuinely empty response still reads as empty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["", "   \n\n  "], ids=["empty", "whitespace"])
def test_a_genuinely_empty_response_still_reads_as_empty(content: str) -> None:
    """AC2: the empty path is untouched."""
    segmentation = segment_reasoning_preamble(content)
    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.NO_BOUNDARY_FOUND
    reasons, failed_rules = _gate(content)
    assert UNRESOLVED_PREAMBLE_CHECK_NAME not in failed_rules
    assert any(_EMPTY_REASON_FRAGMENT in reason for reason in reasons), reasons


# ---------------------------------------------------------------------------
# The guards: an answer behind the trace is still found and passes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [_PAIRED_TRACE_THEN_ANSWER, _UNPAIRED_TRACE_THEN_ANSWER],
    ids=["paired-trace-then-answer", "unpaired-trace-then-answer"],
)
def test_an_answer_behind_the_trace_is_not_refused(content: str) -> None:
    """The narrowing: only a trace with NOTHING behind it is refused."""
    segmentation = segment_reasoning_preamble(content)
    assert (
        segmentation.boundary_rule is not EnumReasoningBoundaryRule.PREAMBLE_UNRESOLVED
    )
    assert output_refusal_for_segmentation(segmentation) is None
    reasons, failed_rules = _gate(content)
    assert not reasons, reasons
    assert not failed_rules


def test_prose_that_mentions_a_trace_tag_is_not_refused() -> None:
    """A closing tag quoted mid-answer, with the answer after it, is not a trace."""
    content = (
        "Models that reason emit a closing </think> tag before the answer. "
        "The gate strips everything up to it and grades what follows."
    )
    segmentation = segment_reasoning_preamble(content)
    assert (
        segmentation.boundary_rule is not EnumReasoningBoundaryRule.PREAMBLE_UNRESOLVED
    )


def test_segmenting_a_refused_trace_again_is_stable() -> None:
    """Idempotent, as the module declares: a second pass changes nothing."""
    first = segment_reasoning_preamble(_PAIRED_TRACE_ONLY)
    second = segment_reasoning_preamble(first.answer)
    assert second == first
