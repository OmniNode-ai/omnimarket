# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelDeliveryReplayInput — the delivery/replay contract input (OMN-14726).

An ordered, self-contained event sequence plus an optional expected result to
compare against. It declares no live-bus dependency — the whole sequence is
carried by value so the projection is computed deterministically in-process.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_delivery_event import (
    ModelDeliveryEvent,
)
from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_replay_expectation import (
    ModelReplayExpectation,
)


class ModelDeliveryReplayInput(BaseModel):
    """Input to the delivery replay projection compute node.

    Attributes:
        correlation_id: Optional correlation ID for this replay (echoed to the
            output for distributed tracing). Does not participate in the
            projection checksum or cursor, so it never affects determinism.
        sequence: The ordered sequence of delivered events to replay.
        expected: Optional expected result. When present, the node reports
            whether the computed projection diverged from it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID | None = Field(
        default=None,
        description="Optional correlation ID for this replay (traced, not hashed).",
    )
    sequence: tuple[ModelDeliveryEvent, ...] = Field(
        default=(),
        description="Ordered sequence of delivered events to replay.",
    )
    expected: ModelReplayExpectation | None = Field(
        default=None,
        description="Optional expected result for divergence comparison.",
    )


__all__ = ["ModelDeliveryReplayInput"]
