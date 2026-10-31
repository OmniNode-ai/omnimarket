# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_owner_summary_effect.handlers import (
    HandlerOwnerSummary,
)


def build_handler_owner_summary() -> HandlerOwnerSummary:
    """Build the handler with its constructor defaults."""
    return HandlerOwnerSummary()
