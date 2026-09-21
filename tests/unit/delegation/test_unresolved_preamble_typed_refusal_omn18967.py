# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18967 AC3: a preamble with no resolvable answer region is refused, not graded.

The segmenter deliberately fails open: when no declared boundary resolves, the
answer IS the whole response and the rule is `no_boundary_found`. That is
correct for a clean response, and it is the module's "nothing is dropped on a
guess" invariant.

It is **wrong for one case**, and that case conflates two very different
outcomes under one rule. When the response OPENS with a declared reasoning
lead-in and no boundary resolves, the whole thing is scratchpad with no answer
behind it. Failing open there hands the caller the scratchpad and hands the
gate the scratchpad to grade, which is how a pure-preamble response scored 1.0.

`no_boundary_found` must therefore mean "there was nothing to strip", and
**never** "there was plenty to strip and I could not find where it ended".
This module proves the two are now distinguishable, and that the second one
produces a typed refusal carrying the contract-declared reason.

The payloads below are real, captured from the live lab endpoint on 2026-09-21
(vLLM 0.27.1, Qwen3.8-27B). Correlation `ae8cd09b-628a-4466-b2c0-6f8174c244bd`
is the run that scored quality 1.0 with `reasoning_preamble_rule: null` while
its response opened with the scratchpad.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_delegation_output_refusal_reason import (
    EnumDelegationOutputRefusalReason,
)
from omnibase_core.enums.enum_delegation_output_shape import (
    EnumDelegationOutputShape,
)

from omnimarket.delegation.reasoning_preamble import (
    EnumReasoningBoundaryRule,
    output_refusal_for_segmentation,
    segment_reasoning_preamble,
)

# Captured 2026-09-21, correlation ae8cd09b-628a-4466-b2c0-6f8174c244bd.
# Opens with a declared lead-in, carries no answer marker, no markdown header
# and no fenced block, and never emits a trace terminator. Truncated here to
# the shape that matters; the full response ran to several hundred words.
CAPTURED_PURE_PREAMBLE = (
    "We need answer user's request. Need output only body text, no preamble. "
    "Need structure three short sections headings What, Why, Evidence. Under "
    "200 words. Need likely include deliberately excluded? User says structure "
    "as three short sections. Need only body. Let's draft concise."
)

# Captured the same day, task class `document`, after the OMN-18967 profile
# landed: the deliverable with nothing in front of it.
CAPTURED_CLEAN_ANSWER = (
    "The projection writer is consuming all incoming messages and committing "
    "offsets successfully, yet the target database table remains completely "
    "empty. This behavior indicates that the write path is silently failing."
)

# A preamble that DOES resolve, so the ordinary strip still owns it. Same
# lead-in, but a declared markdown header marks where the answer begins.
CAPTURED_RESOLVABLE_PREAMBLE = (
    "We need answer user's request. Let's draft concise.\n\n"
    "## What\n\nThe projection writer commits offsets and stores no rows.\n"
)


@pytest.mark.unit
def test_pure_preamble_is_not_reported_as_nothing_to_strip() -> None:
    """The regression: a scratchpad-only response must not read as clean.

    Before this change the rule was `no_boundary_found`, which is the same
    value a clean answer carries, so no consumer could tell them apart.
    """

    segmentation = segment_reasoning_preamble(CAPTURED_PURE_PREAMBLE)

    assert (
        segmentation.boundary_rule is not EnumReasoningBoundaryRule.NO_BOUNDARY_FOUND
    ), (
        "a response that is entirely reasoning scratchpad reported the same "
        "boundary rule as a clean answer, so nothing downstream can refuse it; "
        f"rule={segmentation.boundary_rule}"
    )
    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.PREAMBLE_UNRESOLVED


@pytest.mark.unit
def test_pure_preamble_produces_the_contract_declared_refusal() -> None:
    """The refusal is typed and reuses the core reason, not a local string."""

    refusal = output_refusal_for_segmentation(
        segment_reasoning_preamble(CAPTURED_PURE_PREAMBLE)
    )

    assert refusal is not None, (
        "a scratchpad-only response produced no refusal, so the gate would "
        "grade the scratchpad"
    )
    assert (
        refusal.reason
        is EnumDelegationOutputRefusalReason.AMBIGUOUS_UNMARKED_DELIVERABLE
    )
    assert refusal.contract_failure_reasons, (
        "the refusal names no failing condition, so a reader cannot tell why "
        "the response was refused"
    )


@pytest.mark.unit
def test_the_refusal_carries_the_declared_output_shape() -> None:
    """The shape is the caller's, not a guess made inside the segmenter."""

    refusal = output_refusal_for_segmentation(
        segment_reasoning_preamble(CAPTURED_PURE_PREAMBLE),
        output_shape=EnumDelegationOutputShape.MARKDOWN,
    )

    assert refusal is not None
    assert refusal.output_shape is EnumDelegationOutputShape.MARKDOWN


@pytest.mark.unit
def test_nothing_is_dropped_even_when_refused() -> None:
    """The module invariant holds: the text is still carried, not discarded.

    The refusal is the signal that the text must not be graded or returned as
    the answer. It is not licence to throw the response away, which would make
    the failure undiagnosable.
    """

    segmentation = segment_reasoning_preamble(CAPTURED_PURE_PREAMBLE)

    assert segmentation.answer == CAPTURED_PURE_PREAMBLE
    assert segmentation.preamble == ""


@pytest.mark.unit
def test_a_clean_answer_is_not_refused() -> None:
    """The other direction, and the reason this cannot be a blanket refusal.

    A clean response has no declared lead-in, so it still fails open with
    `no_boundary_found` and no refusal. Refusing every unresolved response
    would reject correct answers, which is the failure mode OMN-18278's
    criterion 2 was reworded to avoid.
    """

    segmentation = segment_reasoning_preamble(CAPTURED_CLEAN_ANSWER)

    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.NO_BOUNDARY_FOUND
    assert segmentation.answer == CAPTURED_CLEAN_ANSWER
    assert output_refusal_for_segmentation(segmentation) is None


@pytest.mark.unit
def test_a_resolvable_preamble_is_still_stripped_and_not_refused() -> None:
    """A preamble with a boundary keeps the old behaviour exactly.

    This is the case the existing rules already handle. It must not become a
    refusal, or the change would trade one over-broad verdict for another.
    """

    segmentation = segment_reasoning_preamble(CAPTURED_RESOLVABLE_PREAMBLE)

    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.MARKDOWN_HEADER
    assert segmentation.answer.startswith("## What")
    assert output_refusal_for_segmentation(segmentation) is None


@pytest.mark.unit
def test_segmenting_a_refused_response_again_is_stable() -> None:
    """Idempotence, which the module promises and every seam relies on."""

    once = segment_reasoning_preamble(CAPTURED_PURE_PREAMBLE)
    twice = segment_reasoning_preamble(once.answer)

    assert twice.boundary_rule is once.boundary_rule
    assert twice.answer == once.answer


@pytest.mark.unit
def test_empty_content_is_not_a_preamble_refusal() -> None:
    """An empty response is an operational outcome, not an output refusal.

    Reporting it here would take a verdict that belongs to the empty-response
    path and attribute it to output shape.
    """

    segmentation = segment_reasoning_preamble("")

    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.NO_BOUNDARY_FOUND
    assert output_refusal_for_segmentation(segmentation) is None


@pytest.mark.unit
def test_a_paired_trace_block_is_not_an_unresolved_preamble() -> None:
    """The first false-refusal guard, found by an existing test going red.

    A paired reasoning block is owned by the ordinary paired-tag strip, which
    removes it and reveals the answer behind it. The lead-in phrase naturally
    sits INSIDE that block, so without this guard a perfectly good answer
    reads as scratchpad with nothing behind it, and the gate refuses it.

    Caught by `test_quality_gate_strips_thinking_traces_before_compile_check`,
    which went red on a `code_generation` response carrying paired tags and
    clean code. Pinned here so the guard has a test naming the reason rather
    than only a distant module that happens to fail.
    """

    paired = (
        "<think>\n"
        "Here's my reasoning process:\n\n"
        "1. The user wants Python code.\n"
        "2. Let me think through edge cases.\n"
        "</think>\n"
        "def add(a: int, b: int) -> int:\n    return a + b\n"
    )

    segmentation = segment_reasoning_preamble(paired)

    assert (
        segmentation.boundary_rule is not EnumReasoningBoundaryRule.PREAMBLE_UNRESOLVED
    ), (
        "a paired trace block was treated as a scratchpad with no deliverable, "
        "so a correct answer behind it would be refused"
    )
    assert output_refusal_for_segmentation(segmentation) is None
