# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production end of the journal: the fleet broker, over the shared publisher."""

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    KafkaEventPublisher,
)


class KafkaObserverEventSink:
    """Builds its connection on first use, so a missing broker config is a
    failed publish (the event stays journaled), not a failed construction."""

    def __init__(self) -> None:
        self._publisher: KafkaEventPublisher | None = None

    def publish(
        self, topic: str, payload: dict[str, object], *, key: str, event_id: str
    ) -> None:
        if self._publisher is None:
            from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

            config = ModelKafkaEventBusConfig().apply_environment_overrides()
            self._publisher = KafkaEventPublisher(config.bootstrap_servers)
        self._publisher.publish(
            topic,
            payload,
            key=key,
            correlation_id=None,
            content_event_id=event_id,
        )
