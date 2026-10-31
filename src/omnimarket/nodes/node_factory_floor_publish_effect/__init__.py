# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local ONEX wrapper for node_factory_floor_publish_effect."""

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
)


class NodeFactoryFloorPublishEffect(HandlerFactoryFloorPublish):
    """ONEX entry-point wrapper for HandlerFactoryFloorPublish."""


__all__ = ["HandlerFactoryFloorPublish", "NodeFactoryFloorPublishEffect"]
