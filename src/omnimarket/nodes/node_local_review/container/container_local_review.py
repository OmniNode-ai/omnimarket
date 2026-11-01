# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_local_review.handlers.handler_local_review import (
    HandlerLocalReview,
)


def build_handler_local_review() -> HandlerLocalReview:
    """Build the handler with its constructor defaults."""
    return HandlerLocalReview()
