# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Utilities for mapping between local intent models and event payloads.

ModelIntentRecord (from omnimemory.handlers.adapters.models) and the
event payload models used for Kafka event transmission.

The key difference between models:
    - ModelIntentRecord.session_ref (str) -> ModelIntentRecordPayload.session_ref (str)
    - ModelIntentRecord.intent_category (str) -> ModelIntentRecordPayload.intent_category (str)
    - ModelIntentRecord.created_at_utc -> ModelIntentRecordPayload.created_at

Example::

    from datetime import datetime, timezone
    from uuid import uuid4

    from omnimemory.handlers.adapters.models import ModelIntentRecord
    from omnimarket.nodes.node_intent_query_effect.utils import map_to_intent_payload

    record = ModelIntentRecord(
        intent_id=uuid4(),
        session_ref="session_abc123",
        intent_category="debugging",
        confidence=0.9,
        keywords=["error", "fix"],
        created_at_utc=datetime.now(tz=timezone.utc),
    )

    payload = map_to_intent_payload(record)
    # payload is ready for event transmission

.. versionadded:: 0.1.0
    Initial implementation for OMN-1504.

.. versionchanged:: 0.2.0
    Updated to use omnibase_core.models.intelligence.ModelIntentRecord
    instead of local domain model (omnibase-core 0.13.1).

.. versionchanged:: 0.3.0
    Uses ModelIntentRecordPayload from omnibase-core 0.17.

.. versionchanged:: 0.4.0
    ModelIntentRecord from omnibase_core.models.intelligence (OMN-1476).

.. versionchanged:: 0.5.0
    Uses local ModelIntentRecord from omnimemory.handlers.adapters.models
    and ModelIntentRecordPayload (renamed from ModelModelIntentRecordPayload) from
    omnibase_core.models.events (omnibase-core 0.18).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC
from typing import TYPE_CHECKING

from omnibase_core.models.events import ModelIntentRecordPayload

if TYPE_CHECKING:
    from omnimemory.handlers.adapters.models import ModelIntentRecord

__all__ = ["map_intent_records", "map_to_intent_payload"]


def map_to_intent_payload(record: ModelIntentRecord) -> ModelIntentRecordPayload:
    """Convert a ModelIntentRecord to ModelIntentRecordPayload.

    Maps from the local intent model to the event payload model
    for transmission in Kafka events.

    Args:
        record: The intent record from AdapterIntentGraph (local model).

    Returns:
        ModelIntentRecordPayload suitable for event transmission.

    Note:
        Field mappings:
            - ModelIntentRecord.session_ref (str) -> ModelIntentRecordPayload.session_ref (str)
            - ModelIntentRecord.intent_category (str) -> ModelIntentRecordPayload.intent_category (str)
            - ModelIntentRecord.created_at_utc -> ModelIntentRecordPayload.created_at
    """
    # Ensure created_at_utc is timezone-aware (UTC) for the payload.
    # Only naive datetimes (tzinfo is None) are patched; already-aware datetimes
    # are passed through unchanged. We do not raise for aware datetimes because
    # the database driver (asyncpg / psycopg2) may return either form depending
    # on connection settings. Naive values are safe to assume UTC here because
    # the database schema stores all timestamps in UTC without tz metadata.
    created_at = record.created_at_utc
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)

    return ModelIntentRecordPayload(
        intent_id=record.intent_id,
        session_ref=record.session_ref,
        intent_category=record.intent_category,
        confidence=record.confidence,
        keywords=list(record.keywords),
        created_at=created_at,
    )


def map_intent_records(
    records: Sequence[ModelIntentRecord],
) -> list[ModelIntentRecordPayload]:
    """Convert a sequence of ModelIntentRecord to ModelIntentRecordPayload.

    Convenience function for bulk conversion of intent records.

    Args:
        records: Sequence of local intent records from AdapterIntentGraph.

    Returns:
        List of payload models for event transmission.
    """
    return [map_to_intent_payload(record) for record in records]
