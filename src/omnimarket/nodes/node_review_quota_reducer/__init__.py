# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review quota reducer: admission of frontier reviews against a preregistered weekly quota.

``HandlerReviewQuotaReducer.handle(ModelReviewQuotaRequest) -> ModelReviewQuotaDecision``
(definition-B). Every covered model call asks it first, so independent producers
cannot multiply a route's quota (OMN-21187).
"""

from omnimarket.nodes.node_review_quota_reducer.handlers.handler_review_quota_reducer import (
    HandlerReviewQuotaReducer,
)


class NodeReviewQuotaReducer(HandlerReviewQuotaReducer):
    """ONEX entry-point wrapper for HandlerReviewQuotaReducer."""


__all__ = ["HandlerReviewQuotaReducer", "NodeReviewQuotaReducer"]
