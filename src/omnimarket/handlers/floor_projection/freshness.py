# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's side of a projection read: the bundle as written, and one projection judged against its own bound.

The age is the producer's ``as_of`` against the time of the floor cycle, never the time the bundle was written, so a
reader that keeps running over a producer that stopped cannot make the data look new.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import JsonValue, ValidationError

from omnimarket.models.floor_projection import ModelFloorProjectionBundle


@dataclass(frozen=True)
class ProjectionPayload:
    """A projection's payload when it is fresh; otherwise the reason it reads unknown."""

    projection_id: str
    payload: dict[str, JsonValue] | None
    reason: str = ""
    as_of: datetime | None = None
    age_s: int | None = None
    producer: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def load_bundle(path: Path) -> ModelFloorProjectionBundle | None:
    """The bundle the reader wrote, or None when it is absent or is not a bundle."""
    try:
        return ModelFloorProjectionBundle.model_validate_json(path.read_bytes())
    except (OSError, ValueError, ValidationError):
        return None


def projection_payload(
    bundle: ModelFloorProjectionBundle | None, projection_id: str, now: datetime
) -> ProjectionPayload:
    """The payload of one projection, or why it reads unknown: no bundle, not declared, unread, or older than its bound."""
    if bundle is None:
        return ProjectionPayload(
            projection_id, None, "the projection reads were not written"
        )
    entry = bundle.entries.get(projection_id)
    if entry is None:
        return ProjectionPayload(
            projection_id, None, f"the reader does not declare {projection_id}"
        )
    if entry.status != "read" or entry.as_of is None:
        return ProjectionPayload(projection_id, None, entry.reason)
    age_s = max(int((now - entry.as_of).total_seconds()), 0)
    if age_s > entry.max_age_s:
        return ProjectionPayload(
            projection_id,
            None,
            f"the projection is {age_s}s old, over its {entry.max_age_s}s bound",
            as_of=entry.as_of,
            age_s=age_s,
            producer=entry.producer,
        )
    return ProjectionPayload(
        projection_id,
        entry.payload,
        "",
        as_of=entry.as_of,
        age_s=age_s,
        producer=entry.producer,
    )
