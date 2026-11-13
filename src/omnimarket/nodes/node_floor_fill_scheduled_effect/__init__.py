# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor fill scheduled effect node."""

from omnimarket.nodes.node_floor_fill_scheduled_effect.handlers.handler_floor_fill_scheduled import (
    HandlerFloorFillScheduled,
)


class NodeFloorFillScheduledEffect(HandlerFloorFillScheduled):
    """The node form of the floor fill scheduled effect handler."""


__all__: list[str] = [
    "NodeFloorFillScheduledEffect",
]
