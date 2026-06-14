# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Real Kafka publish wiring for the standalone emit-daemon runner.

The standalone runner (``__main__.py``) and the in-process omniclaude
lifecycle wrapper both feed a ``publish_fn`` into ``KafkaPublisherLoop``.
The in-process wrapper builds a real ``EventBusKafka`` and routes through
``event_bus.publish``; this module gives the standalone subprocess runner
the same canonical bus path so that ``--kafka-bootstrap-servers`` actually
publishes instead of dropping events through a no-op stub.

Construction order:
    config = ModelKafkaEventBusConfig(bootstrap_servers=...).apply_environment_overrides()
    bus = create_kafka_event_bus(config)
    await bus.start()
    publish_fn = build_kafka_publish_fn(bus, source=...)

The returned callable matches ``PublishFn`` from ``publisher_loop`` exactly:
    async def publish_fn(topic, key, value, headers) -> None
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from omnimarket.nodes.node_emit_daemon.publisher_loop import PublishFn


async def create_kafka_bus(bootstrap_servers: str, *, timeout_seconds: float) -> object:
    """Build and start a real ``EventBusKafka`` from bootstrap settings.

    Mirrors the canonical construction used by the omniclaude in-process
    lifecycle (``_OmnimarketEmitDaemon.start``): build a typed config,
    apply environment overrides, construct via the authorised factory,
    then ``start()``.

    Raises if ``bootstrap_servers`` is empty — a started Kafka bus with no
    brokers is a silent-drop trap and must fail fast.
    """
    if not bootstrap_servers:
        raise ValueError(
            "bootstrap_servers is required to build a Kafka event bus; "
            "omit --kafka-bootstrap-servers entirely to run spool-only"
        )

    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

    environment = os.environ.get("OMNICLAUDE_PUBLISHER_ENVIRONMENT", "")
    config = ModelKafkaEventBusConfig(
        bootstrap_servers=bootstrap_servers,
        environment=environment,
        timeout_seconds=int(timeout_seconds),
    ).apply_environment_overrides()

    bus = EventBusKafka(config)
    await bus.start()
    return bus


def build_kafka_publish_fn(bus: object, *, source: str) -> PublishFn:
    """Adapt a started Kafka bus into the ``KafkaPublisherLoop`` publish_fn.

    The publisher loop hands us ``(topic, key, value, headers)`` where
    ``headers`` is a ``dict[str, str]`` carrying ``source``, ``event_type``,
    ``timestamp`` and ``correlation_id``. We rebuild the typed
    ``ModelEventHeaders`` and route through ``bus.publish`` — the same
    contract the in-process lifecycle wrapper uses.
    """
    from omnibase_infra.event_bus.models import ModelEventHeaders

    async def _publish(
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: dict[str, str],
    ) -> None:
        try:
            correlation_id = UUID(headers["correlation_id"])
        except (KeyError, ValueError):
            correlation_id = uuid4()
        try:
            timestamp = datetime.fromisoformat(headers["timestamp"])
        except (KeyError, ValueError):
            timestamp = datetime.now(UTC)

        event_headers = ModelEventHeaders(
            source=headers.get("source", source),
            event_type=headers.get("event_type", topic),
            timestamp=timestamp,
            correlation_id=correlation_id,
        )
        await bus.publish(  # type: ignore[attr-defined]
            topic=topic,
            key=key,
            value=value,
            headers=event_headers,
        )

    return _publish


__all__: list[str] = [
    "build_kafka_publish_fn",
    "create_kafka_bus",
]
