# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily line scheduled effect node."""

from omnimarket.nodes.node_daily_line_scheduled_effect.handlers.handler_daily_line_scheduled import (
    HandlerDailyLineScheduled,
)


class NodeDailyLineScheduledEffect(HandlerDailyLineScheduled):
    """The node form of the daily line scheduled effect handler."""


__all__: list[str] = [
    "NodeDailyLineScheduledEffect",
]
