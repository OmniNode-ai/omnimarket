# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the local observability sink the effect degrades to (OMN-21695).

The handler never constructs the sink itself: when the runtime injects no Kafka or
Postgres sink it takes the one this module binds.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.nodes.node_observability_sink_effect.models.model_observability_sink_input import (
    ModelActionEvent,
)


class InMemoryObservabilitySink:
    """Local default sink — the contract default when no remote adapter is injected.

    The in-memory/local bus is always present; Kafka and PostgreSQL persistence
    are runtime overrides. When neither override is wired (the default local
    runtime), observability events are recorded in memory so the effect runs
    over the local bus instead of crashing. Trace ids are prefixed ``inmemory:``
    and row ids are deterministic (uuid5 of the event id) so the output honestly
    signals that local persistence was used, not a remote backend.
    """

    def __init__(self) -> None:
        self.kafka_events: list[ModelActionEvent] = []
        self.postgres_events: list[ModelActionEvent] = []

    def publish_action_event(
        self,
        *,
        correlation_id: UUID,
        session_id: UUID,
        event: ModelActionEvent,
    ) -> str:
        self.kafka_events.append(event)
        return f"inmemory:{event.event_id}"

    def insert_action_event(
        self,
        *,
        correlation_id: UUID,
        session_id: UUID,
        event: ModelActionEvent,
    ) -> UUID:
        self.postgres_events.append(event)
        return uuid5(NAMESPACE_URL, f"observability-inmemory:{event.event_id}")


def bind_local_sink() -> InMemoryObservabilitySink:
    """Bind a fresh in-memory sink."""
    return InMemoryObservabilitySink()
