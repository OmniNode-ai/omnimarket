# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claude review key budget: the key that can afford a call and keep its reserve.

``HandlerClaudeReviewBudget.handle(ModelClaudeReviewBudgetRequest) -> ModelClaudeReviewBudgetResult``
(definition-B). The review gateway asks it before each upstream call: no key
means a 503 with no upstream call, so the voter falls through to its overflow
endpoint (OMN-20967).
"""

from omnimarket.nodes.node_claude_review_budget_compute.handlers.handler_claude_review_budget import (
    HandlerClaudeReviewBudget,
)


class NodeClaudeReviewBudgetCompute(HandlerClaudeReviewBudget):
    """ONEX entry-point wrapper for HandlerClaudeReviewBudget."""


__all__ = ["HandlerClaudeReviewBudget", "NodeClaudeReviewBudgetCompute"]
