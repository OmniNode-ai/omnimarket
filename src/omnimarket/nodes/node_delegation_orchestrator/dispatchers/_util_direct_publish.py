# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared direct event-bus publish helper for delegation dispatchers."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus import ProtocolEventBus


async def publish_events_direct(
    events: list[BaseModel],
    correlation_id: UUID,
    event_bus: ProtocolEventBus | None,
    logger: logging.Logger,
    publisher_name: str,
) -> list[BaseModel]:
    """Publish events with a topic to the event bus and return topicless events."""
    if event_bus is None:
        return events

    unpublished: list[BaseModel] = []
    for idx, event in enumerate(events):
        topic = getattr(event, "topic", None)
        if topic is None:
            unpublished.append(event)
            continue
        envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
            envelope_id=uuid5(correlation_id, f"{type(event).__name__}:{idx}"),
            payload=event,
            correlation_id=correlation_id,
            envelope_timestamp=datetime.now(UTC),
        )
        await event_bus.publish_envelope(
            envelope,  # type: ignore[arg-type]
            topic=topic,
        )
        logger.debug(
            "%s published %s to %s (correlation_id=%s)",
            publisher_name,
            type(event).__name__,
            topic,
            str(correlation_id),
        )
    return unpublished
