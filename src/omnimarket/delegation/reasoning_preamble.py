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
    """No declared boundary resolved; the whole response is the answer."""


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

    if offset is None and _has_lead_in(content, policy):
        offset = _answer_marker_offset(content, policy)
        rule = EnumReasoningBoundaryRule.ANSWER_MARKER
        if offset is None:
            offset = _structural_offset(content, _MARKDOWN_HEADER_RE)
            rule = EnumReasoningBoundaryRule.MARKDOWN_HEADER
        if offset is None:
            offset = _structural_offset(content, _FENCE_OPENER_RE)
            rule = EnumReasoningBoundaryRule.FENCED_BLOCK

    if offset is None:
        return _whole(content)

    answer = content[offset:].lstrip()
    if not answer:
        # Everything after the seam is whitespace: the "preamble" was the whole
        # answer. Cutting here would hand the caller nothing, so do not cut.
        return _whole(content)

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


__all__: list[str] = [
    "EnumReasoningBoundaryRule",
    "ModelReasoningSegmentation",
    "segment_reasoning_preamble",
]
