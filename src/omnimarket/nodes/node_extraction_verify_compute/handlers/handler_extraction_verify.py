# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerExtractionVerify: the model proposes, this verifier admits (OMN-21139, TE.2).

Pure and definition-B: ``handle(ModelExtractionVerifyRequest) -> ModelExtractionVerifyResult``,
no event envelope, no model client, no ledger read, no I/O. Plan section 5.1: a proposed
event is admitted only when its quote, after whitespace normalisation, equals the cited
operator-typed prompt's text at the cited offsets. An admitted span is widened to its
enclosing sentence or sentences by one fixed, versioned splitter, so a fragment cannot
lose a negation or a condition. Every other proposal is dropped and counted by reason.
An admitted event is a TRANSCRIPT_EXTRACTS candidate, never a ruling.
"""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import ValidationError

from omnimarket.models.transcript_extraction import (
    EnumCapturedPromptOrigin,
    EnumExtractionRejectionReason,
    ModelCapturedPrompt,
    ModelExtractionRejection,
    ModelExtractionVerifyRequest,
    ModelExtractionVerifyResult,
    ModelProposedExtractionEvent,
    ModelTranscriptExtractArtifact,
)

SPLITTER_VERSION = "sentence-splitter/1"

# sentence-splitter/1: a sentence ends after a run of . ! or ?, with any closing quotes
# or brackets (curly quotes included), when whitespace or the end of the text follows; a newline also ends one.
# Changing this pattern is a new splitter version.
_SENTENCE_END = re.compile(r"[.!?]+[\"'\u201d\u2019)\]]*(?=\s|$)|\n")


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _trimmed(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """The sentences of text as [start, end) offsets, whitespace trimmed, in order.

    Every non-whitespace character of text lies in exactly one span. A text the
    splitter finds no boundary in is one span: the whole prompt, trimmed.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        span = _trimmed(text, start, match.end())
        if span is not None:
            spans.append(span)
        start = match.end()
    span = _trimmed(text, start, len(text))
    if span is not None:
        spans.append(span)
    return spans


def _widen(text: str, start: int, end: int) -> tuple[int, int]:
    """The enclosing sentence or sentences of text[start:end], which holds a non-space."""
    inner = _trimmed(text, start, end)
    if inner is None:  # unreachable: admission compared a non-empty normalised quote
        raise ValueError("cannot widen a whitespace-only span")
    first, last = inner[0], inner[1] - 1
    spans = sentence_spans(text)
    widened_start = next(s for s, e in spans if s <= first < e)
    widened_end = next(e for s, e in spans if s <= last < e)
    return widened_start, widened_end


def compute_candidate_digest(artifact: ModelTranscriptExtractArtifact) -> str:
    """sha256 over the canonical JSON of the candidate's content (plan section 5 item 1).

    The body is kind, quote, source event_id, prompt_id and the admitted offsets only;
    a timestamp or a generated id never enters it, so equal candidates share a digest.
    """
    body = {
        "covers_end": artifact.admitted_end,
        "covers_start": artifact.admitted_start,
        "event_id": artifact.source_ref,
        "kind": artifact.kind.value,
        "prompt_id": artifact.prompt_id,
        "quote": artifact.quote,
    }
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _admit(
    event: ModelProposedExtractionEvent, source: ModelCapturedPrompt
) -> ModelTranscriptExtractArtifact:
    start, end = _widen(source.text, event.covers_start, event.covers_end)
    quote = source.text[start:end]
    draft = ModelTranscriptExtractArtifact(
        kind=event.kind,
        quote=quote,
        quote_sha256=hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        source_ref=source.event_id,
        prompt_id=source.prompt_id,
        proposed_start=event.covers_start,
        proposed_end=event.covers_end,
        admitted_start=start,
        admitted_end=end,
        scope=event.scope,
        splitter_version=SPLITTER_VERSION,
        candidate_digest="0" * 64,
    )
    return draft.model_copy(
        update={"candidate_digest": compute_candidate_digest(draft)}
    )


def _judge(
    event: ModelProposedExtractionEvent,
    sources: dict[tuple[str, str], ModelCapturedPrompt],
) -> ModelTranscriptExtractArtifact | EnumExtractionRejectionReason:
    source = sources.get((event.event_id, event.prompt_id))
    if source is None:
        return EnumExtractionRejectionReason.UNKNOWN_SOURCE
    if source.origin is not EnumCapturedPromptOrigin.OPERATOR_TYPED:
        return EnumExtractionRejectionReason.SYNTHETIC_SOURCE
    quote = _normalise(event.quote)
    if (
        event.covers_end <= len(source.text)
        and _normalise(source.text[event.covers_start : event.covers_end]) == quote
    ):
        return _admit(event, source)
    if quote in _normalise(source.text):
        return EnumExtractionRejectionReason.WRONG_OFFSETS
    return EnumExtractionRejectionReason.ABSENT


class HandlerExtractionVerify:
    """Admit or reject each proposal of one segment against its captured prompts."""

    def handle(
        self, request: ModelExtractionVerifyRequest
    ) -> ModelExtractionVerifyResult:
        sources = {(p.event_id, p.prompt_id): p for p in request.prompts}
        admitted: list[ModelTranscriptExtractArtifact] = []
        rejections: list[ModelExtractionRejection] = []
        counts = dict.fromkeys(EnumExtractionRejectionReason, 0)
        for index, raw in enumerate(request.proposals):
            try:
                event = ModelProposedExtractionEvent.model_validate_json(raw)
            except ValidationError:
                verdict: (
                    ModelTranscriptExtractArtifact | EnumExtractionRejectionReason
                ) = EnumExtractionRejectionReason.PARSE_FAILURE
                prompt_id = None
            else:
                verdict = _judge(event, sources)
                prompt_id = event.prompt_id
            if isinstance(verdict, ModelTranscriptExtractArtifact):
                admitted.append(verdict)
                continue
            counts[verdict] += 1
            rejections.append(
                ModelExtractionRejection(
                    proposal_index=index, reason=verdict, prompt_id=prompt_id
                )
            )
        return ModelExtractionVerifyResult(
            splitter_version=SPLITTER_VERSION,
            admitted=tuple(admitted),
            rejections=tuple(rejections),
            rejection_counts=counts,
        )


__all__ = [
    "SPLITTER_VERSION",
    "HandlerExtractionVerify",
    "compute_candidate_digest",
    "sentence_spans",
]
