# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the integration pass's typed batch from its snapshot topic (OMN-25849).

The hourly integration pass hands its readings to ``node_integration_reading_publish_effect``, which publishes the
projection (``ModelIntegrationProjection``) on the integration floor snapshot topic. This module reads the newest
publication of that topic through the shared snapshot source, refuses a publication that is unread, older than its
declared bound or not a projection, and returns the rows as the ``ModelIntegrationReadingBatch`` the starter selects
from. No ledger file and no environment variable names the input: the topic and the bound are the overlay's
declaration of the projection, the same one the floor's reader uses.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError

from omnimarket.handlers.floor_projection import ProtocolSnapshotSource
from omnimarket.models.floor_projection import ModelFloorProjectionEnvelope
from omnimarket.models.integration_reading import (
    ModelIntegrationProjection,
    ModelIntegrationReadingBatch,
)


class IntegrationBatchRefusedError(Exception):
    """The topic holds no batch this workflow may prove from; the message says why."""


async def read_integration_batch(
    source: ProtocolSnapshotSource,
    *,
    topic: str,
    projection_id: str,
    max_age_s: int,
    now: datetime,
) -> ModelIntegrationReadingBatch:
    """The newest publication of ``projection_id`` on ``topic`` as a reading batch, or a refusal naming the reason."""
    try:
        publications = await source.publications(topic)
    except (
        Exception
    ) as exc:  # the broker being down is a refusal with its reason, not a crash
        raise IntegrationBatchRefusedError(
            f"{topic} cannot be read ({type(exc).__name__}: {exc})"
        ) from exc
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
    if newest is None:
        raise IntegrationBatchRefusedError(
            f"{topic} holds no valid publication of {projection_id}"
            + (f" ({skipped} skipped)" if skipped else "")
        )
    age_s = int((now - newest.as_of).total_seconds())
    if age_s > max_age_s:
        raise IntegrationBatchRefusedError(
            f"the newest {projection_id} batch on {topic} is as of "
            f"{newest.as_of.strftime('%Y-%m-%dT%H:%M:%SZ')} from {newest.producer}, {age_s} s old, "
            f"over the declared {max_age_s} s: the integration pass has published no current batch"
        )
    try:
        projection = ModelIntegrationProjection.model_validate(newest.payload)
    except ValidationError as exc:
        raise IntegrationBatchRefusedError(
            f"the newest {projection_id} batch on {topic} is not an integration projection: {exc.error_count()} errors"
        ) from exc
    return ModelIntegrationReadingBatch(
        as_of=projection.as_of,
        origin=f"{topic} as of {projection.as_of.strftime('%Y-%m-%dT%H:%M:%SZ')} from {newest.producer}",
        readings=tuple(projection.rows.values()),
    )


__all__ = ["IntegrationBatchRefusedError", "read_integration_batch"]
