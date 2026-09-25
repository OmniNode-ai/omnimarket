# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Separate a leaked plain-text reasoning scratchpad from the answer (OMN-18379).

A local model asked to answer a question sometimes answers it AND ships the
scratchpad it used to get there, ahead of the answer, as plain text with no
opening tag. The paired-tag strip in the quality gate cannot see that text, so
two things follow that have nothing to do with the answer: phrase-scanning
blocking rules judge the scratchpad, and the caller's ``result.txt`` opens with
it.

This module owns the boundary and nothing else. The phrases, markers and tags
it searches for are declared in ``task_class_contracts.v1.yaml``
(``reasoning_preamble``), which also documents the search order and why each
entry is there.

The two invariants that make this safe to apply everywhere:

* **Nothing is dropped on a guess.** When no declared boundary resolves, the
  answer IS the whole response and the rule is recorded as
  ``no_boundary_found``, so a reader can tell "there was nothing to strip" from
  "the segmenter never ran".
* **It is idempotent.** Segmenting an answer segment again returns it unchanged,
  so applying it at the verification seam and at the response seam cannot
  compound.
"""

from __future__ import annotations

import re
from enum import StrEnum, unique

from omnibase_core.enums.enum_delegation_output_refusal_reason import (
    EnumDelegationOutputRefusalReason,
)
from omnibase_core.enums.enum_delegation_output_shape import (
    EnumDelegationOutputShape,
)
from omnibase_core.models.delegation.wire.model_delegation_output_refusal import (
    ModelDelegationOutputRefusal,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.inference.task_class_authority import (
    ModelReasoningPreamblePolicy,
    resolve_reasoning_preamble_policy,
)

#: An ATX markdown header at line start: one to six hashes, then a space.
_MARKDOWN_HEADER_RE = re.compile(r"^#{1,6} \S", re.MULTILINE)

#: A fenced-block opener at line start.
_FENCE_OPENER_RE = re.compile(r"^```", re.MULTILINE)

#: How far into a response a lead-in phrase may start and still count as a
#: lead-in. A scratchpad announces itself immediately; a phrase this far in is
#: prose about reasoning, not the start of a reasoning trace.
_LEAD_IN_WINDOW_CHARS: int = 120


@unique
class EnumReasoningBoundaryRule(StrEnum):
    """Which declared rule resolved the boundary between preamble and answer."""

    UNPAIRED_CLOSING_TAG = "unpaired_closing_tag"
    """A reasoning-trace terminator with no matching opener before it."""

    ANSWER_MARKER = "answer_marker"
    """A line equal to a contract-declared answer marker, after a lead-in."""

    MARKDOWN_HEADER = "markdown_header"
    """The first ATX header at line start, after a lead-in."""

    FENCED_BLOCK = "fenced_block"
    """The first fence opener at line start, after a lead-in."""

    NO_BOUNDARY_FOUND = "no_boundary_found"
    """No declared boundary resolved and no lead-in was present.

    This means "there was nothing to strip". It is the clean case.
    """

    PREAMBLE_UNRESOLVED = "preamble_unresolved"
    """The response is declared reasoning with no answer behind it.

    Three shapes resolve here: a declared lead-in opened the response and no
    boundary resolved (OMN-18967); a paired reasoning trace block with nothing
    outside it; and reasoning closed by the model's own unpaired trace
    terminator with nothing after it (both OMN-19434).

    OMN-18967 AC3. This means "there was plenty to strip and no answer was
    found behind it" — the response is scratchpad with no deliverable, not a
    clean answer. Until this member existed both outcomes reported
    ``no_boundary_found``, so no consumer could tell them apart and a
    scratchpad-only response was graded as though it were the answer.

    The answer field still carries the whole response, because the module does
    not drop text on a verdict. :func:`output_refusal_for_segmentation` turns
    this rule into the typed refusal that stops it being graded or returned.
    """


class ModelReasoningSegmentation(BaseModel):
    """The answer, the scratchpad removed from in front of it, and the seam."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str = Field(
        description=(
            "The segment to verify and to hand to the caller. Equals the whole "
            "response when no boundary resolved."
        )
    )
    preamble: str = Field(
        description=(
            "The text removed from in front of the answer, retained verbatim "
            "so a refusal or an acceptance can be audited against what was "
            "actually judged. Empty when no boundary resolved."
        )
    )
    boundary_rule: EnumReasoningBoundaryRule = Field(
        description="Which declared rule resolved the seam."
    )
    boundary_offset: int = Field(
        ge=0,
        description=(
            "Character offset into the ORIGINAL response where the answer "
            "begins, so an auditor can point at the seam rather than trust a "
            "copy of the two halves. Zero when no boundary resolved."
        ),
    )


def _has_lead_in(content: str, policy: ModelReasoningPreamblePolicy) -> bool:
    """Whether the response OPENS with a declared reasoning lead-in."""
    window = content[:_LEAD_IN_WINDOW_CHARS].lower()
    return any(phrase.lower() in window for phrase in policy.lead_in_phrases)


def _unpaired_closing_tag_offset(
    content: str, policy: ModelReasoningPreamblePolicy
) -> int | None:
    """Offset just past the first closing trace tag that has no opener."""
    best: int | None = None
    for closing in policy.closing_trace_tags:
        index = content.find(closing)
        if index == -1:
            continue
        opening = f"<{closing[2:]}"
        if 0 <= content.find(opening) < index:
            # Paired: the ordinary paired-tag strip owns this one.
            continue
        end = index + len(closing)
        if best is None or end < best:
            best = end
    return best


def _has_paired_trace_block(content: str, policy: ModelReasoningPreamblePolicy) -> bool:
    """Whether a PAIRED reasoning block is present, opener and closer both.

    OMN-18967 AC3 guard. A paired block is owned by the ordinary paired-tag
    strip, which removes it and reveals the answer behind it, so its presence
    means a deliverable is reachable even though no boundary rule in this
    module resolved one. ``_unpaired_closing_tag_offset`` deliberately skips a
    paired terminator for exactly that reason.

    Without this, a lead-in phrase sitting INSIDE the paired block — where a
    scratchpad's lead-in naturally sits — would make a perfectly good answer
    look like a scratchpad with nothing behind it.
    """
    for closing in policy.closing_trace_tags:
        index = content.find(closing)
        if index == -1:
            continue
        opening = f"<{closing[2:]}"
        if 0 <= content.find(opening) < index:
            return True
    return False


def _is_paired_trace_only(content: str, policy: ModelReasoningPreamblePolicy) -> bool:
    """Whether the response is paired reasoning blocks and nothing else (OMN-19434).

    Every paired block (declared opener through its closer) is removed; if
    anything but whitespace is left, there is an answer outside the reasoning
    and the ordinary paired-tag strip will reveal it. An empty response is not
    trace-only: it carries no reasoning, and it stays on the empty path.
    """
    remainder = content
    found = False
    for closing in policy.closing_trace_tags:
        opening = f"<{closing[2:]}"
        block = re.compile(re.escape(opening) + r".*?" + re.escape(closing), re.DOTALL)
        remainder, count = block.subn("", remainder)
        found = found or count > 0
    return found and not remainder.strip()


def _answer_marker_offset(
    content: str, policy: ModelReasoningPreamblePolicy
) -> int | None:
    """Offset just past the first line that IS a declared answer marker."""
    offset = 0
    for line in content.splitlines(keepends=True):
        if line.strip() in policy.answer_markers:
            return offset + len(line)
        offset += len(line)
    return None


def _structural_offset(content: str, pattern: re.Pattern[str]) -> int | None:
    """Offset of the first line-start structural boundary, if any."""
    match = pattern.search(content)
    if match is None or match.start() == 0:
        # A boundary at offset 0 means there is no preamble in front of it.
        return None
    return match.start()


def segment_reasoning_preamble(content: str) -> ModelReasoningSegmentation:
    """Split ``content`` into a leaked reasoning preamble and the answer.

    The four boundary rules are tried in the order the contract declares. The
    three structural ones require a declared lead-in phrase at the start of the
    response; the unpaired trace terminator does not, because the model's own
    terminator is unambiguous about where its reasoning ended.

    Returns a segmentation whose ``answer`` is the whole response, whose
    ``preamble`` is empty and whose rule is ``no_boundary_found`` when nothing
    resolves — including when the contract declares no policy at all.
    """
    policy = resolve_reasoning_preamble_policy()
    if policy is None or not content:
        return _whole(content)

    offset = _unpaired_closing_tag_offset(content, policy)
    rule = EnumReasoningBoundaryRule.UNPAIRED_CLOSING_TAG

    # OMN-18967 AC3: remembered so an unresolved boundary can say WHY it is
    # unresolved. A lead-in with no boundary behind it is a different outcome
    # from no lead-in at all, and conflating them is what let a scratchpad-only
    # response reach the gate as an answer.
    # A paired trace block is owned by the ordinary paired-tag strip, which
    # reveals the answer behind it, so this module must not report the
    # response as scratchpad-with-no-deliverable.
    has_lead_in = _has_lead_in(content, policy) and not _has_paired_trace_block(
        content, policy
    )

    if offset is None and has_lead_in:
        offset = _answer_marker_offset(content, policy)
        rule = EnumReasoningBoundaryRule.ANSWER_MARKER
        if offset is None:
            offset = _structural_offset(content, _MARKDOWN_HEADER_RE)
            rule = EnumReasoningBoundaryRule.MARKDOWN_HEADER
        if offset is None:
            offset = _structural_offset(content, _FENCE_OPENER_RE)
            rule = EnumReasoningBoundaryRule.FENCED_BLOCK

    if offset is None:
        # OMN-19434: a paired trace block with nothing outside it is reasoning
        # with no answer, exactly as a lead-in with nothing behind it is. It has
        # no lead-in in the sense above (the paired block is excluded), so it
        # needs its own test; without one it fell through as a clean response,
        # the gate's paired-tag strip removed all of it, and the gate reported
        # "empty response" about a response that was all reasoning.
        if has_lead_in or _is_paired_trace_only(content, policy):
            return _unresolved(content)
        return _whole(content)

    answer = content[offset:].lstrip()
    if not answer:
        # Everything after the seam is whitespace, so there is no answer behind
        # the boundary. Cutting here would hand the caller nothing. A boundary
        # was found and there was still no deliverable, which is the refusal
        # case whether or not a lead-in opened the response: the model's own
        # terminator says where its reasoning ended, and nothing followed it
        # (OMN-19434; before it, the no-lead-in case returned the whole
        # scratchpad and the gate graded it as the answer).
        return _unresolved(content)

    # The offset the receipt carries points at the first character of the
    # answer, not at the whitespace ahead of it.
    answer_offset = len(content) - len(answer)
    return ModelReasoningSegmentation(
        answer=answer,
        preamble=content[:offset],
        boundary_rule=rule,
        boundary_offset=answer_offset,
    )


def _whole(content: str) -> ModelReasoningSegmentation:
    return ModelReasoningSegmentation(
        answer=content,
        preamble="",
        boundary_rule=EnumReasoningBoundaryRule.NO_BOUNDARY_FOUND,
        boundary_offset=0,
    )


def _unresolved(content: str) -> ModelReasoningSegmentation:
    """The whole response, flagged as scratchpad with no deliverable behind it.

    Shaped exactly like :func:`_whole` apart from the rule, deliberately: the
    text is still carried so the failure can be audited, and only the rule
    tells a consumer not to grade it.
    """
    return ModelReasoningSegmentation(
        answer=content,
        preamble="",
        boundary_rule=EnumReasoningBoundaryRule.PREAMBLE_UNRESOLVED,
        boundary_offset=0,
    )


#: The gate check name recorded when no deliverable region resolved.
UNRESOLVED_PREAMBLE_CHECK_NAME = "deliverable_region_resolved"

#: The gate failure reason a scratchpad-only response carries (OMN-18967 AC3).
#:
#: ``WEAK_OUTPUT`` rather than ``MALFORMED``, for the same reason the
#: truncation veto chose it: the reducer escalates on ``WEAK_OUTPUT`` and
#: deliberately does not escalate on ``MALFORMED``. A response that is all
#: reasoning and no answer is precisely the case a costlier rung routinely
#: does answer, so this must CLIMB rather than terminalise. The response is
#: not structurally broken; it simply never got to the deliverable.
UNRESOLVED_PREAMBLE_GATE_FAILURE_REASON = (
    "WEAK_OUTPUT: the response is declared reasoning (a lead-in phrase or a "
    "reasoning trace) and no declared boundary resolved an answer behind it, "
    "so the text is the model's scratchpad rather than the deliverable"
)


def output_refusal_for_segmentation(
    segmentation: ModelReasoningSegmentation,
    *,
    output_shape: EnumDelegationOutputShape = EnumDelegationOutputShape.PLAIN_TEXT,
) -> ModelDelegationOutputRefusal | None:
    """The typed refusal a segmentation implies, or ``None`` if it implies none.

    OMN-18967 AC3. Exactly one segmentation outcome is a refusal:
    :attr:`EnumReasoningBoundaryRule.PREAMBLE_UNRESOLVED`, where the response
    opened with a declared reasoning lead-in and no declared boundary resolved
    an answer behind it. Every other outcome returns ``None`` — including
    ``no_boundary_found``, which is the clean case and must stay accepted.

    That narrowness is the point. Refusing every response whose boundary did
    not resolve would reject correct answers, which is why OMN-18278's
    criterion 2 was recorded as "not met as worded, and should not be". The
    discriminator is the lead-in, not the absence of a boundary.

    The reason is the contract-declared
    :attr:`EnumDelegationOutputRefusalReason.AMBIGUOUS_UNMARKED_DELIVERABLE`
    from omnibase_core rather than a locally minted string, so a consumer
    matches one vocabulary across both the output-shape and schema arms.

    ``output_shape`` is the shape the CALLER declared. It defaults to plain
    text for a caller that declared none; the segmenter never infers it from
    the content, because guessing a shape from a scratchpad would be a second
    heuristic layered on the one that already failed.
    """
    if segmentation.boundary_rule is not EnumReasoningBoundaryRule.PREAMBLE_UNRESOLVED:
        return None
    return ModelDelegationOutputRefusal(
        reason=EnumDelegationOutputRefusalReason.AMBIGUOUS_UNMARKED_DELIVERABLE,
        output_shape=output_shape,
        contract_failure_reasons=(
            "response is declared reasoning (a lead-in phrase or a reasoning trace)",
            "no declared boundary rule resolved an answer region behind it",
        ),
    )


__all__: list[str] = [
    "UNRESOLVED_PREAMBLE_CHECK_NAME",
    "UNRESOLVED_PREAMBLE_GATE_FAILURE_REASON",
    "EnumReasoningBoundaryRule",
    "ModelReasoningSegmentation",
    "output_refusal_for_segmentation",
    "segment_reasoning_preamble",
]
