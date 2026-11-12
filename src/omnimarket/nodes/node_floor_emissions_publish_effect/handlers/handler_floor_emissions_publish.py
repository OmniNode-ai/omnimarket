# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold the floor's emission topics into the floor_emissions projection and publish it, stamped with the time it was read (OMN-23877).

A topic that cannot be read publishes nothing, so the projection ages and the floor reads it unknown, rather than a partial fold being
sent on as the whole. A publication that is not an event is counted and skipped, never read as data.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from omnimarket.handlers.floor_projection import (
    ProtocolSnapshotSource,
    fold_emissions,
    payload_from_emissions,
)
from omnimarket.models.floor_projection import (
    ModelFloorEmissionsOverlay,
    ModelFloorEmissionsPublishResult,
    ModelFloorProjectionEnvelope,
)


class ProtocolEnvelopePublisher(Protocol):
    async def publish(self, envelope: ModelFloorProjectionEnvelope) -> None: ...


class InMemoryEnvelopePublisher:
    """The default: keeps what it was given."""

    def __init__(self) -> None:
        self.published: list[ModelFloorProjectionEnvelope] = []

    async def publish(self, envelope: ModelFloorProjectionEnvelope) -> None:
        self.published.append(envelope)


class HandlerFloorEmissionsPublish:
    """One pass: read the emission topics through the injected source, fold them, publish the projection."""

    def __init__(
        self,
        *,
        source: ProtocolSnapshotSource,
        publisher: ProtocolEnvelopePublisher,
        overlay: ModelFloorEmissionsOverlay,
        producer: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._publisher = publisher
        self._overlay = overlay
        self._producer = producer
        self._clock = clock or (lambda: datetime.now(UTC))

    async def handle(self, request: None = None) -> ModelFloorEmissionsPublishResult:
        as_of = self._clock()
        publications: list[bytes] = []
        for topic in self._overlay.emission_topics:
            try:
                publications.extend(await self._source.publications(topic))
            except Exception as exc:  # the broker being down is a reading, not a crash
                return ModelFloorEmissionsPublishResult(
                    published=False,
                    projection_id=self._overlay.projection_id,
                    reason=f"{topic} cannot be read ({type(exc).__name__})",
                )
        events, skipped = fold_emissions(
            publications,
            since=as_of - timedelta(hours=self._overlay.window_hours),
            max_events=self._overlay.max_events,
        )
        await self._publisher.publish(
            ModelFloorProjectionEnvelope(
                projection_id=self._overlay.projection_id,
                as_of=as_of,
                producer=self._producer,
                payload=payload_from_emissions(
                    events, skipped, self._overlay.window_hours
                ),
            )
        )
        return ModelFloorEmissionsPublishResult(
            published=True,
            projection_id=self._overlay.projection_id,
            as_of=as_of,
            events=len(events),
            skipped=skipped,
        )
