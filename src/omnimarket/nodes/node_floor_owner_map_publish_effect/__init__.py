# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publishes the landing controller's owner map as a floor projection."""

from omnimarket.nodes.node_floor_owner_map_publish_effect.handlers import (
    HandlerFloorOwnerMapPublish,
)


class NodeFloorOwnerMapPublishEffect(HandlerFloorOwnerMapPublish):
    """ONEX entry point."""


__all__ = ["HandlerFloorOwnerMapPublish", "NodeFloorOwnerMapPublishEffect"]
