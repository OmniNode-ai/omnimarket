# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_test_selector_nightly_sender_scheduled_effect: sends the collect and publish commands after each nightly on the runtime tick."""

from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.handlers.handler_nightly_sender_scheduled import (
    HandlerNightlySenderScheduled,
)

__all__ = [
    "HandlerNightlySenderScheduled",
    "NodeTestSelectorNightlySenderScheduledEffect",
]


class NodeTestSelectorNightlySenderScheduledEffect(HandlerNightlySenderScheduled):
    """ONEX entry-point wrapper for HandlerNightlySenderScheduled."""
