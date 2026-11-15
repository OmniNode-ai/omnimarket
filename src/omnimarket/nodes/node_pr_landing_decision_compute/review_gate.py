# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review gate's pure reading: what a PR's recorded review verdicts say about its exact head.

``read_review_gate`` is what the landing decision asks before it merges a new-work head. Only a verdict on the exact
head counts: a head with verdicts only on other heads reads ``superseded``, a new-work head with none reads
``missing``, and a PR with no review fact at all reads ``unread`` when the policy expects the fact, and is decided as
before otherwise. A PR that is not new work is not the gate's to hold, and a CLEAN verdict on the head reads ``pass``.
No clock and no I/O.
"""

from __future__ import annotations

from omnimarket.handlers.review_head_reading import read_head_review
from omnimarket.models.model_landing_review import (
    EnumReviewGateVerdict,
    ModelReviewGateReading,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingLandSkipReason,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingPolicy,
    ModelLandingPrFacts,
)

#: The named reason a held head carries, one per non-pass verdict.
SKIP_FOR_REVIEW: dict[EnumReviewGateVerdict, EnumLandingLandSkipReason] = {
    EnumReviewGateVerdict.MISSING: EnumLandingLandSkipReason.REVIEW_MISSING,
    EnumReviewGateVerdict.CHANGES_NEEDED: EnumLandingLandSkipReason.REVIEW_CHANGES_NEEDED,
    EnumReviewGateVerdict.SUPERSEDED: EnumLandingLandSkipReason.REVIEW_SUPERSEDED,
    EnumReviewGateVerdict.UNREAD: EnumLandingLandSkipReason.REVIEW_UNREAD,
}


def read_review_gate(
    p: ModelLandingPrFacts, policy: ModelLandingPolicy
) -> ModelReviewGateReading | None:
    """The reading of the PR's review gate, or None when the gate holds nothing."""
    if p.review is None:
        if policy.review_read_expected:
            return ModelReviewGateReading(
                verdict=EnumReviewGateVerdict.UNREAD,
                detail="no review fact for the PR",
            )
        return None
    if not p.review.new_work:
        return None
    return read_head_review(p.review, p.head_sha)


__all__ = ["SKIP_FOR_REVIEW", "read_review_gate"]
