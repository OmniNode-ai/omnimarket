# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_test_durations_refresh_effect - keep the test-durations record ahead of its cap (OMN-22389)."""

from omnimarket.nodes.node_test_durations_refresh_effect.handlers.handler_test_durations_refresh import (
    HandlerTestDurationsRefresh,
)


class NodeTestDurationsRefreshEffect(HandlerTestDurationsRefresh):
    """ONEX entry-point wrapper for HandlerTestDurationsRefresh (OMN-22389)."""


__all__ = ["HandlerTestDurationsRefresh", "NodeTestDurationsRefreshEffect"]
