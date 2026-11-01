# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_private_hosted_minutes_watch_effect.handlers import (
    HandlerPrivateHostedMinutesWatch,
)


def build_handler_private_hosted_minutes_watch() -> HandlerPrivateHostedMinutesWatch:
    """Build the handler with its constructor defaults."""
    return HandlerPrivateHostedMinutesWatch()
