# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A producer's one publication of a floor projection on its snapshot topic."""

from __future__ import annotations

from typing import Any

from omnimarket.models.floor_projection import ModelFloorProjectionEnvelope


async def publish_envelope(
    bus: Any, topic: str, envelope: ModelFloorProjectionEnvelope
) -> None:
    """Publish keyed by the projection id, so a compacted topic keeps the newest publication of each projection."""
    await bus.publish(
        topic,
        envelope.projection_id.encode("utf-8"),
        envelope.model_dump_json().encode("utf-8"),
    )
