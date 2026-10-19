# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerExtractionFold: an order-independent fold of segment results (OMN-21139, TE.2).

Pure and definition-B: ``handle(ModelExtractionFoldRequest) -> ModelExtractionFoldResult``.
Overlapping segments admit the same candidate more than once; the fold keeps one per
``candidate_digest`` and orders them by prompt, offsets and digest, so the result is the
same under every arrival order. A segment that arrives twice is folded once (by
``segment_id``; of two different results under one id, the one whose canonical JSON sorts
first is kept, which is itself order-independent). Nothing here is a ruling and nothing
appends to the ledger.
"""

from __future__ import annotations

from omnimarket.models.transcript_extraction import (
    EnumExtractionRejectionReason,
    ModelExtractionFoldRequest,
    ModelExtractionFoldResult,
    ModelExtractionVerifyResult,
    ModelTranscriptExtractArtifact,
)


def _canonical(result: ModelExtractionVerifyResult) -> str:
    return result.model_dump_json()


def _order(artifact: ModelTranscriptExtractArtifact) -> tuple[str, int, int, str]:
    return (
        artifact.prompt_id,
        artifact.admitted_start,
        artifact.admitted_end,
        artifact.candidate_digest,
    )


class HandlerExtractionFold:
    """Fold a transcript's segment results into one candidate list."""

    def handle(self, request: ModelExtractionFoldRequest) -> ModelExtractionFoldResult:
        segments: dict[str, ModelExtractionVerifyResult] = {}
        for result in request.segment_results:
            kept = segments.get(result.segment_id)
            if kept is None or _canonical(result) < _canonical(kept):
                segments[result.segment_id] = result
        candidates: dict[str, ModelTranscriptExtractArtifact] = {}
        counts = dict.fromkeys(EnumExtractionRejectionReason, 0)
        for segment_id in sorted(segments):
            result = segments[segment_id]
            for artifact in result.admitted:
                candidates.setdefault(artifact.candidate_digest, artifact)
            for reason, count in result.rejection_counts.items():
                counts[reason] += count
        return ModelExtractionFoldResult(
            segment_ids=tuple(sorted(segments)),
            candidates=tuple(sorted(candidates.values(), key=_order)),
            rejection_counts=counts,
        )


__all__ = ["HandlerExtractionFold"]
