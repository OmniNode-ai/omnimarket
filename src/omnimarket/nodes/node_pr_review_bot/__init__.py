# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_review_bot — PR Review Bot WorkflowPackage.

Automated PR review with multi-model fan-out, GitHub thread posting,
and judge model verification before thread resolution.
"""

from omnimarket.nodes.node_pr_review_bot.node import HandlerPrReviewBot

__all__ = [
    "HandlerPrReviewBot",
    "NodePrReviewBot",
]


class NodePrReviewBot:
    """ONEX entry-point marker for node_pr_review_bot."""

    __onex_node_type__ = "node_pr_review_bot"
