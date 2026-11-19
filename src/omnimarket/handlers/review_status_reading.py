# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared reading of a head's review-gate commit status.

``read_head_review_status`` is the pure reading both the landing decision and pr-handoff's decision ask of the
statuses read for a pull request. Only a status of the ``fable-review-gate`` context on the exact head counts: the
newest such status decides, ``success`` passes and any other state is failing; with none on the head, a status on
another head reads ``other_head`` and no status at all reads ``missing``. No clock and no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.models.model_review_status import (
    REVIEW_STATUS_CONTEXT,
    EnumReviewStatusVerdict,
    ModelReviewStatusReading,
    ModelReviewStatusRow,
)


def read_head_review_status(
    statuses: Sequence[ModelReviewStatusRow], head_sha: str
) -> ModelReviewStatusReading:
    """The reading of a head's review-gate statuses; a status of another head or context never counts for it."""
    gate = [s for s in statuses if s.context == REVIEW_STATUS_CONTEXT]
    on_head = [s for s in gate if s.head_sha == head_sha]
    if on_head:
        newest = max(reversed(on_head), key=lambda s: s.at)  # ties: the later row
        if newest.state == "success":
            return ModelReviewStatusReading(
                verdict=EnumReviewStatusVerdict.PASS,
                detail=f"{REVIEW_STATUS_CONTEXT} success on the head {head_sha[:12]}",
            )
        return ModelReviewStatusReading(
            verdict=EnumReviewStatusVerdict.FAILING,
            detail=(
                f"{REVIEW_STATUS_CONTEXT} is {newest.state} on the head {head_sha[:12]}, "
                "not success"
            ),
        )
    if gate:
        return ModelReviewStatusReading(
            verdict=EnumReviewStatusVerdict.OTHER_HEAD,
            detail=(
                f"{REVIEW_STATUS_CONTEXT} exists only on other heads, none on {head_sha[:12]}: "
                "a status on an older head does not count"
            ),
        )
    return ModelReviewStatusReading(
        verdict=EnumReviewStatusVerdict.MISSING,
        detail=f"no {REVIEW_STATUS_CONTEXT} status on the head {head_sha[:12]}",
    )


__all__ = ["read_head_review_status"]
