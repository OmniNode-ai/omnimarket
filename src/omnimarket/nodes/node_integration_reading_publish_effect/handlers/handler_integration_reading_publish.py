# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish the integration projection as the ``integration`` floor projection (OMN-23899).

An effect that executes one typed command and reports: it publishes the projection it is handed, stamped with the
projection's own ``as_of`` (the time the pass stood by its readings), and decides nothing. The projection is built by
``node_integration_projection_compute``; the runtime module composes the two.
"""

from __future__ import annotations

from pydantic import JsonValue

from omnimarket.models.floor_projection import (
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionPublishResult,
)
from omnimarket.models.integration_reading import ModelIntegrationPublishCommand
from omnimarket.nodes.node_floor_owner_map_publish_effect.handlers import (
    ProtocolEnvelopePublisher,
)


class HandlerIntegrationReadingPublish:
    """One publication of the projection it is handed, through the injected publisher."""

    def __init__(self, *, publisher: ProtocolEnvelopePublisher, producer: str) -> None:
        self._publisher = publisher
        self._producer = producer

    async def handle(
        self, request: ModelIntegrationPublishCommand
    ) -> ModelFloorProjectionPublishResult:
        projection = request.projection
        payload: dict[str, JsonValue] = projection.model_dump(mode="json")
        await self._publisher.publish(
            ModelFloorProjectionEnvelope(
                projection_id=request.projection_id,
                as_of=projection.as_of,
                producer=self._producer,
                payload=payload,
            )
        )
        return ModelFloorProjectionPublishResult(
            published=True,
            projection_id=request.projection_id,
            as_of=projection.as_of,
            entries=len(projection.rows),
        )


__all__ = ["HandlerIntegrationReadingPublish"]
