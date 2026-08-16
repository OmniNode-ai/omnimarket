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
    async def publish_fn(topic, key, value, headers) -> ModelPublishReceipt | None

OMN-15861: this adapter is the seam. It used to swallow ``bus.publish``'s return
value, which meant the publisher loop could not tell where a record landed even
once the bus started reporting it. It now propagates the
``ModelPublishReceipt`` through unchanged, and forwards the loop's
``idempotency_key`` header onto the typed ``ModelEventHeaders`` so the receipt
carries the logical-event identity a readback or projection confirm needs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from omnibase_infra.event_bus.models import ModelPublishReceipt
from omnibase_infra.protocols.protocol_confirmation_strategy import (
    ProtocolConfirmationStrategy,
)

from omnimarket.nodes.node_emit_daemon.confirmation_binding import (
    build_confirmation_bindings,
)
from omnimarket.nodes.node_emit_daemon.models.model_durability import (
    EnumDurabilityTier,
)
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

    # OMN-16049: same defect as node_event_emit_effect's bus factory --
    # ``OMNICLAUDE_PUBLISHER_ENVIRONMENT`` is unset on our deployments, so this
    # passed "" and the config model rejects empty ("environment cannot be
    # empty"), overriding its own valid ``default="local"``. Leave the field
    # unset so the default stands and the runtime's STANDARD source
    # (``KAFKA_ENVIRONMENT``, applied below) governs it.
    config = ModelKafkaEventBusConfig(
        bootstrap_servers=bootstrap_servers,
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
    ) -> ModelPublishReceipt | None:
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
            idempotency_key=headers.get("idempotency_key"),
        )
        receipt = await bus.publish(  # type: ignore[attr-defined]
            topic=topic,
            key=key,
            value=value,
            headers=event_headers,
        )
        return cast("ModelPublishReceipt | None", receipt)

    return _publish


def build_kafka_confirmation_bindings(
    bus: object,
) -> dict[EnumDurabilityTier, ProtocolConfirmationStrategy]:
    """Bind readback confirmation for duty-critical traffic on a started bus.

    Duty-critical records are confirmed by ``BrokerReadbackStrategy`` over a
    ``KafkaReadbackSource`` built from the SAME config the bus publishes with --
    so the coordinate is read back off the cluster that assigned it. Telemetry
    keeps the cheap ``PublishReturnOnlyStrategy``; a readback round trip per
    telemetry event is a real cost this codebase already avoids elsewhere.

    Args:
        bus: A started ``EventBusKafka``. Its public ``config`` property
            supplies bootstrap servers and auth for the readback consumer.

    Raises:
        DurabilityPolicyError: If the resulting binding would let duty-critical
            traffic ack on the publish return (defence in depth -- the guard
            lives in ``build_confirmation_bindings``).
    """
    from omnibase_infra.event_bus.confirmation import (
        BrokerReadbackStrategy,
        KafkaReadbackSource,
    )

    # Readback must use the publishing bus's exact cluster+auth, so it is built
    # from the same config object the bus publishes with.
    config = bus.config  # type: ignore[attr-defined]
    return build_confirmation_bindings(
        duty_critical=BrokerReadbackStrategy(KafkaReadbackSource(config)),
    )


__all__: list[str] = [
    "build_kafka_confirmation_bindings",
    "build_kafka_publish_fn",
    "create_kafka_bus",
]
