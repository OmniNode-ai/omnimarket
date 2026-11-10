# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A producer's one publication of a floor projection on its snapshot topic."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.models.floor_projection import (
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionPublishOverlay,
)


async def publish_envelope(
    bus: Any, topic: str, envelope: ModelFloorProjectionEnvelope
) -> None:
    """Publish keyed by the projection id, so a compacted topic keeps the newest publication of each projection."""
    await bus.publish(
        topic,
        envelope.projection_id.encode("utf-8"),
        envelope.model_dump_json().encode("utf-8"),
    )


async def publish_with_overlay(
    overlay: ModelFloorProjectionPublishOverlay,
    env: Mapping[str, str],
    topic: str,
    envelope: ModelFloorProjectionEnvelope,
) -> bool:
    """Publish on the bus the overlay names. The in-memory bus is the default and holds the publication nowhere: False."""
    if overlay.bus == "inmemory":
        return False
    bootstrap = overlay.kafka_bootstrap
    async with open_lane_bus(
        bus="kafka",
        lane=None if bootstrap else overlay.bus_lane,
        kafka_bootstrap=bootstrap,
        root=workspace_root(env),
    ) as bus:
        await publish_envelope(bus, topic, envelope)
    return True
