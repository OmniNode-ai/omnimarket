# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelDeliveryEvent — a single delivered event in a replayable sequence.

A delivery event carries both the delivery coordinates
(``topic``/``partition``/``offset``) that define the replay **cursor** and the
logical content (``key``/``event_type``/``payload``) that defines the replayed
**projection**. The model is frozen and self-contained: it declares no live-bus
dependency, so a delivery sequence can be captured, serialized, and replayed
entirely in-process. This is the precondition for the B6 canary-acceptance gate
(OMN-14726): replaying the same sequence must yield the same projection checksum
+ cursor, and a divergent sequence must differ.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Shallow, non-recursive JSON alias (Pydantic-safe, mypy-clean). Kept local so
# the node has no cross-node private import (omnimarket boundary rule).
JsonType = dict[str, object] | list[object] | str | int | float | bool | None


class ModelDeliveryEvent(BaseModel):
    """A single delivered event in a replayable sequence.

    Attributes:
        topic: The topic the event was delivered on.
        partition: The partition within the topic (delivery coordinate).
        offset: The offset within the partition (delivery coordinate). The
            per-``(topic, partition)`` maximum offset forms the replay cursor.
        key: The entity/partition key. Drives the last-write-wins materialized
            projection view.
        event_type: The logical event type.
        payload: The JSON-serializable event body. Content that participates in
            the ordered projection checksum.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(
        ..., min_length=1, description="Topic the event was delivered on."
    )
    partition: int = Field(
        default=0, ge=0, description="Partition within the topic (delivery coordinate)."
    )
    offset: int = Field(
        ..., ge=0, description="Offset within the partition (delivery coordinate)."
    )
    key: str = Field(
        default="",
        description="Entity/partition key driving last-write-wins projection.",
    )
    event_type: str = Field(..., min_length=1, description="Logical event type.")
    payload: JsonType = Field(default=None, description="JSON-serializable event body.")


__all__ = ["JsonType", "ModelDeliveryEvent"]
