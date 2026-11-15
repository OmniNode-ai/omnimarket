# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared head review reading: what a head's recorded review verdicts say about that exact head.

``read_head_review`` is the pure reading both the landing decision's review gate and the review-gate context
decision ask of a head's verdicts. Only a verdict on the exact head counts: a head with verdicts only on
other heads reads ``superseded``, a head with none reads ``missing``, and a CLEAN verdict on the head reads
``pass``. No clock and no I/O.
"""

from __future__ import annotations

from omnimarket.models.model_landing_review import (
    EnumReviewGateVerdict,
    EnumReviewVerdict,
    ModelLandingReviewFacts,
    ModelReviewGateReading,
)


def read_head_review(
    review: ModelLandingReviewFacts | None, head_sha: str
) -> ModelReviewGateReading:
    """The reading of a head's review verdicts; ``missing`` when there is no review fact at all."""
    if review is None:
        return ModelReviewGateReading(
            verdict=EnumReviewGateVerdict.MISSING,
            detail="no review verdict recorded for the head",
        )
    on_head = [r for r in review.verdicts if r.head_sha == head_sha]
    if on_head:
        newest = max(reversed(on_head), key=lambda r: r.at)  # ties: the later row
        if newest.verdict is EnumReviewVerdict.CLEAN:
            return ModelReviewGateReading(
                verdict=EnumReviewGateVerdict.PASS,
                detail=f"review CLEAN on the head {head_sha[:12]}",
            )
        return ModelReviewGateReading(
            verdict=EnumReviewGateVerdict.CHANGES_NEEDED,
            detail=f"review changes needed on the head {head_sha[:12]}",
        )
    if review.verdicts:
        return ModelReviewGateReading(
            verdict=EnumReviewGateVerdict.SUPERSEDED,
            detail="review verdicts exist only for other heads",
        )
    return ModelReviewGateReading(
        verdict=EnumReviewGateVerdict.MISSING,
        detail="no review verdict recorded for the head",
    )


__all__ = ["read_head_review"]
