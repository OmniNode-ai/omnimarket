# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The publish seam: one record in, one per-record delivery outcome out."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelPublishOutcome,
    ModelWireRecord,
)


@runtime_checkable
class ProtocolRecordPublisher(Protocol):
    """Publishes one record and reports its own outcome, never a producer-wide flush result."""

    @property
    def message_max_bytes(self) -> int:
        """The `message.max.bytes` the producer was built with (what it enforces)."""
        ...

    async def publish(self, record: ModelWireRecord) -> ModelPublishOutcome: ...


__all__ = ["ProtocolRecordPublisher"]
