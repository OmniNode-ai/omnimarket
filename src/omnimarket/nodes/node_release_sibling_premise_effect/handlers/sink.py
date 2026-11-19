# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verdict sinks of the release sibling-premise read effect.

One verdict per read travels on the verdict topic; one typed failure per read that
could not be answered travels on the failure topic. The in-memory sink keeps them
for tests and the in-memory profile; the bus sink wraps each payload in an event
envelope typed for its topic, as ``adapter_dev_merge_redeploy_host`` publishes its
receipts.
"""

from __future__ import annotations

from uuid import UUID

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.lab_work.bus import _bytes


class InMemoryVerdictSink:
    """Verdicts and failures kept in memory (the in-memory default profile)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object], UUID]] = []

    async def publish(
        self, topic: str, payload: dict[str, object], correlation_id: UUID
    ) -> None:
        """Record ``payload`` as published on ``topic`` with ``correlation_id``."""
        self.published.append((topic, payload, correlation_id))


class BusVerdictSink:
    """Verdicts and failures published on the lab bus as typed envelopes."""

    def __init__(self, bus: ProtocolLabRunBus) -> None:
        self._bus = bus

    async def publish(
        self, topic: str, payload: dict[str, object], correlation_id: UUID
    ) -> None:
        """Wrap ``payload`` in an envelope typed for ``topic`` and put it on the bus."""
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=payload,
            event_type=event_type_for(topic),
            correlation_id=correlation_id,
        )
        await self._bus.publish(
            topic, str(correlation_id).encode("utf-8"), _bytes(envelope)
        )


__all__ = [
    "BusVerdictSink",
    "InMemoryVerdictSink",
]
