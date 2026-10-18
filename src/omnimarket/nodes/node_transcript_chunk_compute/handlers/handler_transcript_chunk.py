# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTranscriptChunk: a lossless chunker with offsets (OMN-21139, TE.2, ports AP-D3, AP-B4).

Pure and definition-B: ``handle(ModelTranscriptChunkRequest) -> ModelTranscriptChunkResult``.
The chunk cores partition ``[0, len)`` of the text, so joining them gives the original
back; each window adds up to the declared overlap before its core, so a span no longer
than the overlap lies whole in at least one window. A cut never falls inside a code fence
or a table, no chunk is dropped by a size or word-count filter, and chunk ids are hashes
of each window's offsets and text, identical on rerun. Each core ends before the end of
the text or at it, and the loop stops there, so it always terminates.
"""

from __future__ import annotations

import bisect
import hashlib
import re

from omnimarket.models.transcript_extraction import (
    ModelTranscriptChunk,
    ModelTranscriptChunkRequest,
    ModelTranscriptChunkResult,
)

# Cut-scoring from the archived adaptive chunker: confidence * 0.7 + proximity * 0.3,
# where proximity is how close the cut is to the end of the budget.
_CONFIDENCE_WEIGHT = 0.7
_PROXIMITY_WEIGHT = 0.3
_PARAGRAPH = 1.0
_LINE = 0.6
_SENTENCE = 0.4
_SPACE = 0.1
_ANY = 0.0
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*\s$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _protected_ranges(text: str) -> list[tuple[int, int]]:
    """Code fences and tables as disjoint [start, end) ranges, in order, never cut inside."""
    ranges: list[tuple[int, int]] = []
    offset = 0
    fence_start: int | None = None
    fence_marker = ""
    table_start: int | None = None
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        match = _FENCE.match(line)
        if fence_start is None and match:
            if table_start is not None:  # a fence ends an open table
                ranges.append((table_start, offset))
                table_start = None
            fence_start, fence_marker = offset, match.group(1)
        elif fence_start is not None and stripped.startswith(fence_marker):
            ranges.append((fence_start, offset + len(line)))
            fence_start = None
        if fence_start is None and not match:
            if stripped.startswith("|"):
                table_start = offset if table_start is None else table_start
            elif table_start is not None:
                ranges.append((table_start, offset))
                table_start = None
        offset += len(line)
    if table_start is not None:
        ranges.append((table_start, offset))
    if fence_start is not None:  # an unclosed fence runs to the end of the text
        ranges.append((fence_start, offset))
    return ranges


def _confidence(text: str, cut: int) -> float:
    """How good a boundary the position before text[cut] is."""
    if text[cut - 2 : cut] == "\n\n":
        return _PARAGRAPH
    if text[cut - 1] == "\n":
        return _LINE
    if _SENTENCE_END.search(text, max(0, cut - 6), cut):
        return _SENTENCE
    if text[cut - 1].isspace():
        return _SPACE
    return _ANY


class _Protected:
    """Membership test for a cut strictly inside one of the disjoint, ordered ranges."""

    def __init__(self, ranges: list[tuple[int, int]]) -> None:
        self.ranges = ranges
        self._starts = [start for start, _ in ranges]

    def forbids(self, cut: int) -> bool:
        index = bisect.bisect_left(self._starts, cut) - 1
        return index >= 0 and cut < self.ranges[index][1]


def _next_cut(
    text: str, start: int, max_chars: int, protected: _Protected
) -> tuple[int, bool]:
    """The end of the core that begins at start, and whether it is oversize."""
    length = len(text)
    if length - start <= max_chars:
        return length, False
    limit = start + max_chars
    cuts = [c for c in range(start + 1, limit + 1) if not protected.forbids(c)]
    if cuts:
        late = [c for c in cuts if c - start >= max_chars // 2] or cuts

        def score(cut: int) -> tuple[float, int]:
            proximity = (cut - start) / max_chars
            value = _CONFIDENCE_WEIGHT * _confidence(text, cut)
            return value + _PROXIMITY_WEIGHT * proximity, cut

        return max(late, key=score), False
    # No cut is allowed inside the budget: the protected block is kept whole.
    for block_start, block_end in protected.ranges:
        if block_start < limit < block_end and block_end > start:
            return block_end, True
    return length, True  # unreachable: a disallowed cut lies inside a protected range


def _chunk_id(start: int, end: int, window: str) -> str:
    digest = hashlib.sha256(f"{start}:{end}:".encode() + window.encode("utf-8"))
    return "chunk_" + digest.hexdigest()


class HandlerTranscriptChunk:
    """Split one transcript text into overlapping windows over a partition of cores."""

    def handle(
        self, request: ModelTranscriptChunkRequest
    ) -> ModelTranscriptChunkResult:
        text = request.text
        protected = _Protected(_protected_ranges(text))
        chunks: list[ModelTranscriptChunk] = []
        core_start = 0
        while core_start < len(text):
            core_end, oversize = _next_cut(
                text, core_start, request.max_chars, protected
            )
            start = max(0, core_start - request.overlap_chars)
            window = text[start:core_end]
            chunks.append(
                ModelTranscriptChunk(
                    chunk_id=_chunk_id(start, core_end, window),
                    index=len(chunks),
                    start=start,
                    end=core_end,
                    core_start=core_start,
                    core_end=core_end,
                    text=window,
                    oversize=oversize,
                )
            )
            core_start = core_end
        return ModelTranscriptChunkResult(
            needs_chunking=len(text) > request.max_chars, chunks=tuple(chunks)
        )


__all__ = ["HandlerTranscriptChunk"]
