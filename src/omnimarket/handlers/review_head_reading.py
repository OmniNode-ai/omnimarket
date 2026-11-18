# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared head review reading: what a head's recorded review verdicts say about that exact head.

``read_head_review`` is the pure reading both the landing decision's review gate and the review-gate context decision ask of a head's verdicts. Only a verdict
on the exact head counts: a head with verdicts only on other heads reads ``superseded``, a head with none reads
``missing``, and a CLEAN verdict on the head reads ``pass``. A CHANGES_NEEDED verdict is read by its findings: any
blocking finding (a failing check, or a cited file and line with a defect) reads ``changes_needed``; findings that
are all advisory read ``pass`` and the reading carries them for post-merge follow-up; and a CHANGES_NEEDED verdict
that lists no finding at all stays ``changes_needed`` (fail closed: a verdict row from before findings existed has
none). No clock and no I/O.
"""

from __future__ import annotations

from omnimarket.handlers.review_finding_class import classify_finding
from omnimarket.models.model_landing_review import (
    EnumReviewFindingClass,
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
        blocking = [
            f
            for f in newest.findings
            if classify_finding(f) is EnumReviewFindingClass.BLOCKING
        ]
        if blocking:
            return ModelReviewGateReading(
                verdict=EnumReviewGateVerdict.CHANGES_NEEDED,
                detail=(
                    f"review changes needed on the head {head_sha[:12]}: "
                    f"{len(blocking)} blocking finding(s)"
                ),
            )
        if newest.findings:
            return ModelReviewGateReading(
                verdict=EnumReviewGateVerdict.PASS,
                detail=(
                    f"review changes needed on the head {head_sha[:12]} lists only "
                    f"{len(newest.findings)} advisory finding(s): post-merge follow-ups"
                ),
                advisory_findings=newest.findings,
            )
        return ModelReviewGateReading(
            verdict=EnumReviewGateVerdict.CHANGES_NEEDED,
            detail=(
                f"review changes needed on the head {head_sha[:12]}: no finding listed"
            ),
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
