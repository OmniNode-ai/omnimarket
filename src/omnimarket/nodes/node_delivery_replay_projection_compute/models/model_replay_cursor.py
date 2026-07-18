# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelReplayCursor — terminal delivery position of a replayed sequence.

The cursor is the terminal *delivery position* after a sequence is replayed:
for each ``(topic, partition)`` it records the maximum offset observed plus the
event count. It is deliberately order-insensitive, which makes it orthogonal to
the projection checksum: a reordered sequence keeps the same cursor but yields a
different projection checksum, while a dropped/added/re-offset event moves the
cursor. The stable ``token`` is a canonical string used for equality and
divergence comparison.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_delivery_position import (
    ModelDeliveryPosition,
)


class ModelReplayCursor(BaseModel):
    """Terminal delivery position of a replayed sequence.

    Attributes:
        positions: Per-``(topic, partition)`` terminal offsets, sorted
            deterministically by ``(topic, partition)``.
        event_count: Number of events consumed to reach this cursor.
        token: Canonical, stable string representation of ``positions`` +
            ``event_count`` used for equality and divergence comparison.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    positions: tuple[ModelDeliveryPosition, ...] = Field(
        default=(),
        description="Per-(topic, partition) terminal offsets (sorted).",
    )
    event_count: int = Field(
        default=0, ge=0, description="Number of events consumed to reach this cursor."
    )
    token: str = Field(
        ...,
        min_length=1,
        description="Canonical stable token for equality/divergence comparison.",
    )


__all__ = ["ModelReplayCursor"]
