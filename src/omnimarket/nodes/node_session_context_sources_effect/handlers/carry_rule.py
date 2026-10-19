# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The carry rule for OPERATOR_WORDS_UNRECORDED (plan section 5 item 2 and decision D10).

Pure. For each operator prompt, in capture order:

- a prompt inside a range an operator review row names leaves the carry set (REVIEWED);
- otherwise the spans covered by rows citing its prompt id are removed; a prompt whose residual
  is empty or whitespace is dropped as recorded (RECORDED);
- any other prompt is carried whole and verbatim (CARRIED), annotated with the rows that cover
  which spans. A lone symbol or emoji is a residual, so it carries the whole prompt.

A citation whose span falls outside its prompt covers nothing, and a review range whose ends
are not both prompts of the session, in order, names nothing: a malformed row never drops words.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from omnimarket.nodes.node_session_context_sources_effect.models import (
    EnumCarryDisposition,
    ModelCarryDecision,
    ModelPromptCitation,
    ModelReviewedRange,
    ModelSessionPrompt,
)


def residual(text: str, citations: Iterable[ModelPromptCitation]) -> str:
    """The text with every in-bounds covered span removed."""
    covered = [False] * len(text)
    for citation in citations:
        if citation.end > len(text):
            continue
        for index in range(citation.start, citation.end):
            covered[index] = True
    return "".join(char for char, hit in zip(text, covered, strict=True) if not hit)


def _reviewed(
    prompts: Sequence[ModelSessionPrompt], ranges: Iterable[ModelReviewedRange]
) -> dict[str, str]:
    position = {prompt.prompt_id: index for index, prompt in enumerate(prompts)}
    named: dict[str, str] = {}
    for item in ranges:
        first = position.get(item.first_prompt_id)
        last = position.get(item.last_prompt_id)
        if first is None or last is None or last < first:
            continue
        for prompt in prompts[first : last + 1]:
            named.setdefault(prompt.prompt_id, item.row_ref)
    return named


def carry(
    prompts: Sequence[ModelSessionPrompt],
    citations: Iterable[ModelPromptCitation],
    reviewed_ranges: Iterable[ModelReviewedRange] = (),
) -> tuple[ModelCarryDecision, ...]:
    """One decision per prompt, in capture order."""
    ordered = sorted(prompts, key=lambda prompt: prompt.capture_sequence)
    by_prompt: dict[str, list[ModelPromptCitation]] = {}
    for citation in citations:
        by_prompt.setdefault(citation.prompt_id, []).append(citation)
    reviewed = _reviewed(ordered, reviewed_ranges)
    decisions: list[ModelCarryDecision] = []
    for prompt in ordered:
        if prompt.prompt_id in reviewed:
            decisions.append(
                ModelCarryDecision(
                    prompt=prompt,
                    disposition=EnumCarryDisposition.REVIEWED,
                    reviewed_by=reviewed[prompt.prompt_id],
                )
            )
            continue
        cited = tuple(
            sorted(
                by_prompt.get(prompt.prompt_id, ()),
                key=lambda c: (c.start, c.end, c.row_ref),
            )
        )
        recorded = bool(cited) and not residual(prompt.text, cited).strip()
        decisions.append(
            ModelCarryDecision(
                prompt=prompt,
                disposition=(
                    EnumCarryDisposition.RECORDED
                    if recorded
                    else EnumCarryDisposition.CARRIED
                ),
                covered_by=cited,
            )
        )
    return tuple(decisions)


__all__ = ["carry", "residual"]
