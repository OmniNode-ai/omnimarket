# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDeliveryReplayProjection — deterministic replay projection (OMN-14726, B6).

Pure COMPUTE. Folds an ordered, self-contained delivery sequence into a
deterministic projection and reports two orthogonal signals:

- ``projection_checksum`` — a sha256 over the ordered fold of the sequence (a
  hash chain of the per-event canonical encodings plus a last-write-wins
  materialized view). Sensitive to content **and** ordering.
- ``cursor`` — the terminal delivery position: per-``(topic, partition)`` max
  offset plus the event count. Sensitive to delivery completeness/position
  (dropped/added/re-offset events) but insensitive to ordering.

No I/O, no clock, no randomness. Given the same input it always produces
byte-identical output — exactly the property the B6 canary-acceptance gate
asserts:

    same sequence  -> identical projection_checksum + cursor
    divergent seq  -> different projection_checksum and/or cursor
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Literal

from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_delivery_event import (
    JsonType,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_delivery_position import (
    ModelDeliveryPosition,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_delivery_replay_input import (
    ModelDeliveryReplayInput,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_replay_cursor import (
    ModelReplayCursor,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_replay_projection import (
    ModelReplayProjection,
)

logger = logging.getLogger(__name__)

# Record separator fed between per-event encodings so event boundaries are
# significant in the hash chain (prevents concatenation collisions).
_RECORD_SEPARATOR = b"\x1e"

# Divergence reason labels.
_REASON_CHECKSUM = "projection_checksum"
_REASON_CURSOR = "cursor"


def _canonical_json(obj: object) -> str:
    """Serialize ``obj`` to a canonical, stable JSON string.

    Sorting keys and eliding insignificant whitespace guarantees a single byte
    representation for any semantically equal object, which is the basis for a
    deterministic checksum. ``default=str`` coerces any non-JSON-native scalar
    (e.g. ``UUID``) to a stable string form.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def project_delivery_sequence(
    request: ModelDeliveryReplayInput,
) -> ModelReplayProjection:
    """Project a delivery sequence into a deterministic checksum + cursor. PURE.

    The fold is order-sensitive for the checksum (a sha256 hash chain over the
    per-event canonical encodings, plus a sorted last-write-wins materialized
    view) and order-insensitive for the cursor (per-``(topic, partition)`` max
    offset + event count). When ``request.expected`` is supplied, the result
    reports whether the projection diverged and on which signal.
    """
    history = hashlib.sha256()
    by_key: dict[str, JsonType] = {}
    max_offsets: dict[tuple[str, int], int] = {}

    for event in request.sequence:
        encoded = _canonical_json(
            {
                "topic": event.topic,
                "partition": event.partition,
                "offset": event.offset,
                "key": event.key,
                "event_type": event.event_type,
                "payload": event.payload,
            }
        )
        history.update(encoded.encode("utf-8"))
        history.update(_RECORD_SEPARATOR)

        # Last-write-wins materialized view keyed by entity key.
        by_key[event.key] = event.payload

        # Cursor is the max offset per (topic, partition).
        tp = (event.topic, event.partition)
        current = max_offsets.get(tp)
        if current is None or event.offset > current:
            max_offsets[tp] = event.offset

    event_count = len(request.sequence)

    # Projection state = ordered history digest + sorted materialized view.
    state = {
        "history": history.hexdigest(),
        "by_key": {key: by_key[key] for key in sorted(by_key)},
        "event_count": event_count,
    }
    projection_checksum = hashlib.sha256(
        _canonical_json(state).encode("utf-8")
    ).hexdigest()

    positions = tuple(
        ModelDeliveryPosition(topic=topic, partition=partition, offset=offset)
        for (topic, partition), offset in sorted(max_offsets.items())
    )
    token = _canonical_json(
        {
            "positions": [[pos.topic, pos.partition, pos.offset] for pos in positions],
            "event_count": event_count,
        }
    )
    cursor = ModelReplayCursor(
        positions=positions, event_count=event_count, token=token
    )

    compared = request.expected is not None
    reasons: list[str] = []
    if request.expected is not None:
        if projection_checksum != request.expected.projection_checksum:
            reasons.append(_REASON_CHECKSUM)
        if token != request.expected.cursor_token:
            reasons.append(_REASON_CURSOR)

    return ModelReplayProjection(
        correlation_id=request.correlation_id,
        projection_checksum=projection_checksum,
        cursor=cursor,
        event_count=event_count,
        compared=compared,
        diverged=bool(reasons),
        divergence_reasons=tuple(reasons),
    )


class HandlerDeliveryReplayProjection:
    """Pure COMPUTE handler: deterministic delivery replay projection + cursor."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self,
        request: ModelDeliveryReplayInput,
    ) -> ModelReplayProjection:
        result = project_delivery_sequence(request)
        logger.info(
            "delivery_replay_projection: %d event(s), checksum=%s, cursor=%s, "
            "compared=%s, diverged=%s",
            result.event_count,
            result.projection_checksum[:12],
            result.cursor.token,
            result.compared,
            result.diverged,
        )
        return result


__all__ = [
    "HandlerDeliveryReplayProjection",
    "project_delivery_sequence",
]
