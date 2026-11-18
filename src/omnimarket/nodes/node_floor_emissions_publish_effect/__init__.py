# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publishes the floor's chain emissions as a floor projection."""

from omnimarket.nodes.node_floor_emissions_publish_effect.handlers import (
    HandlerFloorEmissionsPublish,
)


class NodeFloorEmissionsPublishEffect(HandlerFloorEmissionsPublish):
    """ONEX entry point."""


__all__ = ["HandlerFloorEmissionsPublish", "NodeFloorEmissionsPublishEffect"]
