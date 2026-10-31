# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local ONEX wrapper for node_factory_floor_compute."""

from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    HandlerFactoryFloor,
)


class NodeFactoryFloorCompute(HandlerFactoryFloor):
    """ONEX entry-point wrapper for HandlerFactoryFloor."""


__all__ = ["HandlerFactoryFloor", "NodeFactoryFloorCompute"]
