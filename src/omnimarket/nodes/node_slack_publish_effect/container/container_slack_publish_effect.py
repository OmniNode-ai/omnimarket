# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler factories for node_slack_publish_effect."""

from __future__ import annotations

from omnimarket.nodes.node_slack_publish_effect.handlers.handler_slack_publish_effect import (
    HandlerSlackPublishEffect,
)

__all__ = ["create_handler_slack_publish_effect"]


def create_handler_slack_publish_effect() -> HandlerSlackPublishEffect:
    """Build the handler with its default dependencies."""
    return HandlerSlackPublishEffect()
