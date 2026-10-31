# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_pr_title_check_compute.handlers.handler_pr_title_check import (
    HandlerPrTitleCheck,
)


def build_handler_pr_title_check() -> HandlerPrTitleCheck:
    """Build the handler with its constructor defaults."""
    return HandlerPrTitleCheck()
