# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime-bus forwarder omnibase_core's RuntimeLocal calls after a run on a runtime-local in-memory bus (OMN-24003).

Registered under the ``onex.runtime_bus_forwarders`` entry-point group. ``RuntimeLocal`` hands it the events its
in-memory bus recorded (``ModelRuntimeBusEvent`` items: topic, key_b64, value_b64, headers) and reads back a receipt
mapping (forwarded, failed, detail). The events are read by attribute, so this module imports nothing from the core
models it is handed and a core that predates them is no obstacle. Every failure raises ``RuntimeError``, which core
records as a degraded bus reading; nothing here ends a finished run.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_runtime_bus_forward_effect.handlers.handler_runtime_bus_forward import (
    HandlerRuntimeBusForward,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.models.model_runtime_bus_forward import (
    ModelForwardEvent,
    ModelRuntimeBusForwardCommand,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.overlay import (
    ModelRuntimeBusForwardOverlay,
    load_overlay,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.protocols.protocol_runtime_bus_publisher import (
    ProtocolRuntimeBusPublisher,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.publishers.publisher_kafka import (
    KafkaRuntimeBusPublisher,
    broker_for_lane,
)


class ProtocolRecordedEvent(Protocol):
    """The four fields of a recorded runtime-bus event this forwarder reads."""

    @property
    def topic(self) -> str: ...

    @property
    def key_b64(self) -> str | None: ...

    @property
    def value_b64(self) -> str: ...

    @property
    def headers(self) -> Mapping[str, str]: ...


class CoreRuntimeBusForwarder:
    """Carries a runtime-local run's recorded events to the lane the node's overlay declares."""

    def __init__(
        self,
        *,
        overlay: ModelRuntimeBusForwardOverlay | None = None,
        publisher_for_broker: Callable[[ModelNodeBroker], ProtocolRuntimeBusPublisher]
        | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._overlay = overlay
        self._publisher_for_broker = (
            publisher_for_broker or KafkaRuntimeBusPublisher.from_broker
        )
        self._environ = os.environ if environ is None else environ

    def forward(self, events: Sequence[ProtocolRecordedEvent]) -> dict[str, object]:
        """The core hook: publish ``events`` to the hosted bus and answer the receipt mapping."""
        try:
            overlay = self._overlay or load_overlay(self._environ)
            broker = broker_for_lane(overlay.event_bus.lane, self._environ)
            publisher = self._publisher_for_broker(broker)
        except Exception as exc:
            raise RuntimeError(
                f"runtime bus forwarder could not reach the hosted bus: {type(exc).__name__}: {exc}"
            ) from exc
        command = ModelRuntimeBusForwardCommand(
            run_id=f"runtime-local-{uuid.uuid4()}",
            events=tuple(
                ModelForwardEvent(
                    topic=event.topic,
                    key_b64=event.key_b64,
                    value_b64=event.value_b64,
                    headers=dict(event.headers),
                )
                for event in events
            ),
        )
        return (
            HandlerRuntimeBusForward(overlay=overlay, publisher=publisher)
            .handle(command)
            .receipt()
        )
