# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_test_durations_refresh_effect.handlers.handler_test_durations_refresh import (
    HandlerTestDurationsRefresh,
)


def build_handler_test_durations_refresh() -> HandlerTestDurationsRefresh:
    """Build the handler with its constructor defaults."""
    return HandlerTestDurationsRefresh()
