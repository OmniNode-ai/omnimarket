# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_omniclaude_pin_bump_compute.handlers.handler_omniclaude_pin_bump import (
    HandlerOmniclaudePinBump,
)


def build_handler_omniclaude_pin_bump() -> HandlerOmniclaudePinBump:
    """Build the handler with its constructor defaults."""
    return HandlerOmniclaudePinBump()
