# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish the landing controller's owner map as a floor projection, stamped with the time it was read (OMN-23391).

The controller's host reads its own state and publishes; nothing else reads that file for the floor. A state that cannot be
read publishes nothing, so the projection ages and the floor reads it unknown, rather than a stale map being sent on as new.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from omnimarket.handlers.floor_projection import LaneOwners
from omnimarket.models.floor_projection import (
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionPublishRequest,
    ModelFloorProjectionPublishResult,
)


class ProtocolEnvelopePublisher(Protocol):
    async def publish(self, envelope: ModelFloorProjectionEnvelope) -> None: ...


class InMemoryEnvelopePublisher:
    """The default: keeps what it was given."""

    def __init__(self) -> None:
        self.published: list[ModelFloorProjectionEnvelope] = []

    async def publish(self, envelope: ModelFloorProjectionEnvelope) -> None:
        self.published.append(envelope)


class HandlerFloorOwnerMapPublish:
    """One pass: read the controller's state through the injected reader, publish the map."""

    def __init__(
        self,
        *,
        read_owners: Callable[[Path], LaneOwners | None],
        publisher: ProtocolEnvelopePublisher,
        projection_id: str,
        producer: str,
        state_name: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._read_owners = read_owners
        self._publisher = publisher
        self._projection_id = projection_id
        self._producer = producer
        self._state_name = state_name
        self._clock = clock or (lambda: datetime.now(UTC))

    async def handle(
        self, request: ModelFloorProjectionPublishRequest
    ) -> ModelFloorProjectionPublishResult:
        owners = self._read_owners(Path(request.source_dir) / self._state_name)
        if owners is None:
            return ModelFloorProjectionPublishResult(
                published=False,
                projection_id=self._projection_id,
                reason="the controller's state cannot be read",
            )
        as_of = self._clock()
        await self._publisher.publish(
            ModelFloorProjectionEnvelope(
                projection_id=self._projection_id,
                as_of=as_of,
                producer=self._producer,
                payload=owners.payload(),
            )
        )
        return ModelFloorProjectionPublishResult(
            published=True,
            projection_id=self._projection_id,
            as_of=as_of,
            entries=len(owners.landing) + len(owners.cause) + len(owners.parked),
        )
