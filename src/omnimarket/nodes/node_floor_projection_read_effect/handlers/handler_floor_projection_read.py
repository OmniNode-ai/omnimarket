# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the projections the floor takes from the bus, keep each one's own time, and say why one could not be read (OMN-23391).

Per projection the newest publication by ``as_of`` wins. A publication that does not parse, names another projection or
carries a bare time is counted and skipped, never read as data. A projection with no valid publication, and a topic the
broker could not be read for, is ``unread`` with its reason: the floor reads it as unknown, never as the last value.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.handlers.floor_projection import ProtocolSnapshotSource
from omnimarket.models.floor_projection import (
    ModelFloorProjectionBundle,
    ModelFloorProjectionEntry,
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionReadOverlay,
    ModelFloorProjectionTickRequest,
)


class HandlerFloorProjectionRead:
    """The reads of one pass, over a snapshot source and the overlay's projections."""

    def __init__(
        self,
        *,
        source: ProtocolSnapshotSource,
        overlay: ModelFloorProjectionReadOverlay,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._overlay = overlay
        self._clock = clock or (lambda: datetime.now(UTC))
        self.skipped: dict[str, int] = {}

    async def handle(
        self, request: ModelFloorProjectionTickRequest
    ) -> ModelFloorProjectionBundle:
        read_at = self._clock()
        entries: dict[str, ModelFloorProjectionEntry] = {}
        for projection_id, spec in self._overlay.projections.items():
            try:
                publications = await self._source.publications(spec.topic)
            except Exception as exc:  # the broker being down is a reading, not a crash
                entries[projection_id] = ModelFloorProjectionEntry(
                    projection_id=projection_id,
                    status="unread",
                    max_age_s=spec.max_age_s,
                    read_at=read_at,
                    reason=f"{spec.topic} cannot be read ({type(exc).__name__})",
                )
                continue
            newest: ModelFloorProjectionEnvelope | None = None
            skipped = 0
            for raw in publications:
                try:
                    envelope = ModelFloorProjectionEnvelope.model_validate_json(raw)
                except (ValueError, ValidationError):
                    skipped += 1
                    continue
                if envelope.projection_id != projection_id:
                    skipped += 1
                    continue
                if newest is None or envelope.as_of >= newest.as_of:
                    newest = envelope
            self.skipped[projection_id] = skipped
            if newest is None:
                entries[projection_id] = ModelFloorProjectionEntry(
                    projection_id=projection_id,
                    status="unread",
                    max_age_s=spec.max_age_s,
                    read_at=read_at,
                    reason=f"{spec.topic} holds no valid publication of {projection_id}"
                    + (f" ({skipped} skipped)" if skipped else ""),
                )
                continue
            entries[projection_id] = ModelFloorProjectionEntry(
                projection_id=projection_id,
                status="read",
                max_age_s=spec.max_age_s,
                read_at=read_at,
                as_of=newest.as_of,
                producer=newest.producer,
                payload=newest.payload,
            )
        return ModelFloorProjectionBundle(read_at=read_at, entries=entries)
