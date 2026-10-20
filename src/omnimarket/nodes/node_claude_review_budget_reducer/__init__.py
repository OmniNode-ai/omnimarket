# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure review budget reducer; persisted by the admission effect before granting a call."""

from omnimarket.nodes.node_claude_review_budget_reducer.handlers.handler_review_budget_reducer import (
    HandlerClaudeReviewBudgetReducer,
)


class NodeClaudeReviewBudgetReducer(HandlerClaudeReviewBudgetReducer):
    """Declarative entry point."""


__all__ = ["HandlerClaudeReviewBudgetReducer", "NodeClaudeReviewBudgetReducer"]
