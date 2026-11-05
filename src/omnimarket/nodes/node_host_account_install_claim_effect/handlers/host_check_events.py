# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outbox delivery ports; a failed broker acknowledgement leaves the row pending."""

from collections.abc import Mapping
from uuid import NAMESPACE_URL, uuid5

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.models.host_checks import ModelHostCheckEvent


class MemoryHostCheckEvents:
    def __init__(self, principal: str = "") -> None:
        self.principal = principal
        self.events: list[ModelHostCheckEvent] = []

    def publish(self, event: ModelHostCheckEvent) -> None:
        self.events.append(event)


class KafkaHostCheckEvents:
    def __init__(self, config: Mapping[str, str]) -> None:
        from confluent_kafka import KafkaException, Producer

        self.principal = config["sasl.username"]
        self._timeout_seconds = float(config["message.timeout.ms"]) / 1000
        try:
            self._producer = Producer(dict(config))
        except KafkaException:
            raise OSError("host-check broker binding failed") from None

    def publish(self, event: ModelHostCheckEvent) -> None:
        from confluent_kafka import KafkaException

        failures: list[str] = []
        acknowledged: list[bool] = []

        def delivered(error: object, message: object) -> None:
            if error is not None:
                failures.append("broker refused host-check event")
            else:
                acknowledged.append(True)

        envelope = ModelEventEnvelope[object](
            payload=event.payload,
            event_type=event.topic,
            correlation_id=uuid5(NAMESPACE_URL, event.key),
        )
        try:
            self._producer.produce(
                event.topic,
                key=event.key.encode(),
                value=envelope.model_dump_json().encode(),
                on_delivery=delivered,
            )
            self._producer.flush(self._timeout_seconds)
        except (KafkaException, BufferError):
            raise OSError("host-check event was not acknowledged") from None
        if failures or not acknowledged:
            raise OSError("host-check event was not acknowledged")
