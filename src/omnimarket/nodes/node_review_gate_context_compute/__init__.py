# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review-gate context compute: the pure verdict of a required review check for one pull request head.

``HandlerReviewGateContext.handle(ModelReviewGateContextRequest) -> ModelReviewGateContextDecision`` (definition-B).
The head passes only on a CLEAN review verdict recorded for that exact head, or when the pull request belongs to a
declared bypass class; every other head is a failure with a named reason. No clock and no I/O.
"""

from omnimarket.nodes.node_review_gate_context_compute.handlers.handler_review_gate_context import (
    HandlerReviewGateContext,
    decide_review_gate_context,
)


class NodeReviewGateContextCompute(HandlerReviewGateContext):
    """ONEX entry-point wrapper for HandlerReviewGateContext."""


__all__ = [
    "HandlerReviewGateContext",
    "NodeReviewGateContextCompute",
    "decide_review_gate_context",
]
