# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local ONEX wrapper for node_factory_floor_effect."""

from omnimarket.nodes.node_factory_floor_effect.handlers.handler_factory_floor_effect import (
    HandlerFactoryFloorEffect,
)


class NodeFactoryFloorEffect(HandlerFactoryFloorEffect):
    """ONEX entry-point wrapper for HandlerFactoryFloorEffect."""


__all__ = ["HandlerFactoryFloorEffect", "NodeFactoryFloorEffect"]
