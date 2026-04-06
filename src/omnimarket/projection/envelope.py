"""ONEX envelope unwrapping -- matches omnidash TypeScript parseMessage() exactly."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def unwrap_envelope(raw_bytes: bytes) -> dict[str, Any] | None:
    """Parse a Kafka message value and unwrap ONEX envelope.

    Replicates the omnidash read-model-consumer.ts parseMessage() logic:
    - { payload: { ... } } -> use payload, attach _envelope
    - { data: { ... } } -> use data, attach _envelope, _event_type, _correlation_id
    - Otherwise use raw parsed object
    """
    try:
        raw = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(raw, dict):
        return None

    # Unwrap payload envelope
    if isinstance(raw.get("payload"), dict):
        result = dict(raw["payload"])
        result["_envelope"] = raw
        return result

    # Unwrap data envelope (if data is a dict, not a list)
    data = raw.get("data")
    if isinstance(data, dict):
        result = dict(data)
        result["_envelope"] = raw
        result["_event_type"] = raw.get("event_type")
        result["_correlation_id"] = raw.get("correlation_id")
        return result

    return raw
