# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ONEX entrypoint for agent review dispatch, bound to its handler by contract."""

from .handlers.handler_agent_review_dispatch import HandlerAgentReviewDispatch


class NodeAgentReviewDispatchOrchestrator(HandlerAgentReviewDispatch):
    """Review one pull request head and publish its stored, signed receipt."""


__all__ = ["NodeAgentReviewDispatchOrchestrator"]
