# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_dev_tag_pin_mover_compute.handlers.handler_dev_tag_pin_mover import (
    HandlerDevTagPinMover,
)


def build_handler_dev_tag_pin_mover() -> HandlerDevTagPinMover:
    """Build the handler with its constructor defaults."""
    return HandlerDevTagPinMover()
