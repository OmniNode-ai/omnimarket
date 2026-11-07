# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe consumer: in memory (a given outcome) or a real consumer on the lane's broker.

The real one starts a consumer in a fresh group under the granted prefix on an existing topic and times
``start()`` (connect, join, first assignment), the step that timed out in 30 seconds on the dev broker.
It commits no offset, creates no topic, and leaves the group when it closes.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING
from uuid import uuid4

from omnimarket.nodes.node_broker_load_reading_effect.models.model_broker_load import (
    EnumConsumerStart,
    ModelConsumerStart,
)
from omnimarket.nodes.node_broker_load_reading_effect.overlay import (
    ModelConsumerProbe,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
)

if TYPE_CHECKING:
    from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig


class InMemoryConsumerStart:
    """The consumer's start ends the given way, whatever the deadline."""

    def __init__(self, outcome: ModelConsumerStart) -> None:
        self.outcome = outcome

    def measure(self, deadline_seconds: float) -> ModelConsumerStart:
        return self.outcome


class KafkaConsumerStart:
    """Start one consumer on the broker of ``probe.lane`` and time it."""

    def __init__(self, probe: ModelConsumerProbe) -> None:
        self._probe = probe

    def _config(self) -> ModelKafkaEventBusConfig:
        # Imported here: the in-memory backend must not need a Kafka client.
        from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding
        from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

        selection, transport = resolve_lane_binding(self._probe.lane)
        config = ModelKafkaEventBusConfig.default()
        overrides: dict[str, object] = {
            "bootstrap_servers": selection.bootstrap_servers,
            "security_protocol": selection.security_protocol,
            "sasl_mechanism": selection.sasl_mechanism,
        }
        if transport is not None:
            overrides.update(transport.as_client_config_overrides())
        return config.model_copy(update=overrides)

    def measure(self, deadline_seconds: float) -> ModelConsumerStart:
        try:
            return asyncio.run(self._measure(deadline_seconds))
        except Exception as exc:  # a transport that cannot open is unreadable, not slow
            return ModelConsumerStart(
                outcome=EnumConsumerStart.UNREADABLE,
                detail=reason_token(type(exc).__name__),
            )

    async def _measure(self, deadline_seconds: float) -> ModelConsumerStart:
        from omnibase_infra.event_bus.kafka_transport import KafkaTransport

        transport = KafkaTransport(
            config=self._config(),
            group=self._probe.group_prefix + uuid4().hex,
            topics=(self._probe.probe_topic,),
            auto_offset_reset="latest",
        )
        started = time.monotonic()
        try:
            await asyncio.wait_for(transport.start(), timeout=deadline_seconds)
        except TimeoutError:
            return ModelConsumerStart(
                outcome=EnumConsumerStart.TIMEOUT,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        else:
            return ModelConsumerStart(
                outcome=EnumConsumerStart.STARTED,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            # closing a half-started consumer must not hide the measurement
            with contextlib.suppress(Exception):
                await asyncio.wait_for(transport.close(), timeout=10)


__all__ = ["InMemoryConsumerStart", "KafkaConsumerStart"]
