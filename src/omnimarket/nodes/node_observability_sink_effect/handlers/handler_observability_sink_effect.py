# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerObservabilitySinkEffect — runtime-routed observability sink.

This node replaces the direct ActionLogger + Postgres writes made inline by the
observability skill.  All observability I/O routes through the dispatch bus;
the inline bus bypass in the skill is the architectural violation this node
corrects.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.nodes.node_observability_sink_effect.models.model_observability_sink_input import (
    ModelActionEvent,
    ModelObservabilitySinkInput,
)
from omnimarket.nodes.node_observability_sink_effect.models.model_observability_sink_output import (
    ModelObservabilitySinkOutput,
)


class ProtocolObservabilityKafkaSink(Protocol):
    """Typed boundary for Kafka persistence owned by runtime wiring."""

    def publish_action_event(
        self,
        *,
        correlation_id: UUID,
        session_id: UUID,
        event: ModelActionEvent,
    ) -> str | Awaitable[str]: ...


class ProtocolObservabilityPostgresSink(Protocol):
    """Typed boundary for PostgreSQL persistence owned by runtime wiring."""

    def insert_action_event(
        self,
        *,
        correlation_id: UUID,
        session_id: UUID,
        event: ModelActionEvent,
    ) -> UUID | Awaitable[UUID]: ...


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


class HandlerObservabilitySinkEffect:
    """EFFECT: persist observability events through injected runtime adapters."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        *,
        kafka_sink: ProtocolObservabilityKafkaSink | None = None,
        postgres_sink: ProtocolObservabilityPostgresSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # The in-memory/local sink is the contract default. Kafka/Postgres
        # adapters are overrides supplied by runtime wiring; absent them the
        # effect degrades to local persistence rather than raising.
        local_default = InMemoryObservabilitySink()
        self._kafka_sink: ProtocolObservabilityKafkaSink = (
            kafka_sink if kafka_sink is not None else local_default
        )
        self._postgres_sink: ProtocolObservabilityPostgresSink = (
            postgres_sink if postgres_sink is not None else local_default
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    async def handle(
        self, request: ModelObservabilitySinkInput
    ) -> ModelObservabilitySinkOutput:
        kafka_trace_ids: list[str] = []
        postgres_row_ids: list[UUID] = []

        for event in request.events:
            if request.sink_kafka:
                kafka_trace_ids.append(
                    await _maybe_await(
                        self._kafka_sink.publish_action_event(
                            correlation_id=request.correlation_id,
                            session_id=request.session_id,
                            event=event,
                        )
                    )
                )
            if request.sink_postgres:
                postgres_row_ids.append(
                    await _maybe_await(
                        self._postgres_sink.insert_action_event(
                            correlation_id=request.correlation_id,
                            session_id=request.session_id,
                            event=event,
                        )
                    )
                )

        persisted_count = (
            len(request.events) if request.sink_kafka or request.sink_postgres else 0
        )
        return ModelObservabilitySinkOutput(
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            persisted_event_count=persisted_count,
            kafka_trace_ids=tuple(kafka_trace_ids),
            postgres_row_ids=tuple(postgres_row_ids),
            persisted_at=self._clock(),
        )


async def _maybe_await[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


__all__: list[str] = [
    "HandlerObservabilitySinkEffect",
    "InMemoryObservabilitySink",
    "ProtocolObservabilityKafkaSink",
    "ProtocolObservabilityPostgresSink",
]
