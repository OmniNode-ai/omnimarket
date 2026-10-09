# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shadow review effect node (OMN-20422).

EFFECT: one tick of the shadow reviewer experiment. Reviews each newly observed
in-scope PR with the pre-registered arms and writes the results to the
experiment store on the lab host. Never posts or blocks.
"""

from omnimarket.nodes.node_shadow_review_effect.handlers.handler_shadow_review import (
    HandlerShadowReview,
)
from omnimarket.nodes.node_shadow_review_effect.models.model_shadow_review import (
    ModelShadowReviewCandidate,
    ModelShadowReviewPolicy,
    ModelShadowReviewRequest,
    ModelShadowReviewResult,
)


class NodeShadowReviewEffect(HandlerShadowReview):
    """ONEX entry-point wrapper for HandlerShadowReview."""


__all__ = [
    "HandlerShadowReview",
    "ModelShadowReviewCandidate",
    "ModelShadowReviewPolicy",
    "ModelShadowReviewRequest",
    "ModelShadowReviewResult",
    "NodeShadowReviewEffect",
]
