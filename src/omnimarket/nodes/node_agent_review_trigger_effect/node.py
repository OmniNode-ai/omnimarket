# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative review-trigger node; its contract owns routing."""

from .handlers.handler_review_trigger import HandlerAgentReviewTrigger


class NodeAgentReviewTriggerEffect(HandlerAgentReviewTrigger):
    """Publish a single logical review request for an eligible head."""
