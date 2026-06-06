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
from uuid import UUID

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
        self._kafka_sink = kafka_sink
        self._postgres_sink = postgres_sink
        self._clock = clock or (lambda: datetime.now(UTC))

    async def handle(
        self, request: ModelObservabilitySinkInput
    ) -> ModelObservabilitySinkOutput:
        if request.sink_kafka and self._kafka_sink is None:
            raise RuntimeError(
                "observability sink requested Kafka persistence, but no "
                "ProtocolObservabilityKafkaSink adapter was injected"
            )
        if request.sink_postgres and self._postgres_sink is None:
            raise RuntimeError(
                "observability sink requested PostgreSQL persistence, but no "
                "ProtocolObservabilityPostgresSink adapter was injected"
            )

        kafka_trace_ids: list[str] = []
        postgres_row_ids: list[UUID] = []

        for event in request.events:
            if request.sink_kafka:
                if self._kafka_sink is None:  # pragma: no cover - guarded above
                    raise RuntimeError("Kafka sink adapter is not configured")
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
                if self._postgres_sink is None:  # pragma: no cover - guarded above
                    raise RuntimeError("PostgreSQL sink adapter is not configured")
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
    "ProtocolObservabilityKafkaSink",
    "ProtocolObservabilityPostgresSink",
]
