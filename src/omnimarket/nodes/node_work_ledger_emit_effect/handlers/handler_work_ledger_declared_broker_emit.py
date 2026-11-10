# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Emit the work-ledger event through the broker the node's contract overlay declares (OMN-23452).

The broker and its client transport come only from the node's declared ``event_bus`` overlay
(``type: kafka, lane: <id>``), resolved by ``resolve_dialable_broker``. No ``KAFKA_*`` environment
variable is read for the broker or its security protocol; the SASL identity a host holds only
supplies the credential of the lane the declaration already selected.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from omnimarket.handlers.bus_binding.node_broker import (
    NodeBrokerResolutionError,
    resolve_dialable_broker,
)
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
    KafkaEventPublisher,
    ProtocolPublishAdapter,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox

NODE_NAME: Final[str] = "node_work_ledger_emit_effect"


def declared_kafka_bus(binding: ModelNodeBroker) -> Any:
    """The Kafka bus for a declared broker, built only from the declaration (no env override)."""
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

    password = (
        binding.sasl_password.get_secret_value()
        if binding.sasl_password is not None
        else None
    )
    config = ModelKafkaEventBusConfig(
        bootstrap_servers=binding.bootstrap_servers,
        security_protocol=binding.security_protocol or "PLAINTEXT",
        sasl_mechanism=binding.sasl_mechanism,
        sasl_plain_username=binding.sasl_username,
        sasl_plain_password=password,
    )
    return EventBusKafka(config)


class HandlerWorkLedgerDeclaredBrokerEmit(HandlerEventEmitEffect):
    """Emit the work-ledger event through the broker the node's overlay declares."""

    def __init__(
        self,
        *,
        broker: Callable[[str], ModelNodeBroker] = resolve_dialable_broker,
        bus_factory: Callable[[ModelNodeBroker], Any] | None = None,
        spool: SpoolOutbox | None = None,
        spool_dir: Path | None = None,
    ) -> None:
        super().__init__(spool=spool, spool_dir=spool_dir)
        self._broker = broker
        self._bus_factory = (
            bus_factory if bus_factory is not None else declared_kafka_bus
        )

    def _build_default_adapter(self) -> ProtocolPublishAdapter | None:
        if self._spool_only_opt_out():
            return None
        binding = self._broker(NODE_NAME)
        if binding.bootstrap_servers is None:
            raise NodeBrokerResolutionError(
                f"{NODE_NAME} resolved to a bus with no broker address "
                f"(overlay {binding.overlay_path}); declare `event_bus: {{type: kafka, lane: <lane id>}}`."
            )
        publish_identity = (
            f"{binding.bootstrap_servers}|{binding.security_protocol or ''}|"
            f"{binding.sasl_username or ''}"
        )
        return KafkaEventPublisher(
            binding.bootstrap_servers,
            publish_identity=publish_identity,
            bus_factory=lambda: self._bus_factory(binding),
        )


__all__ = [
    "NODE_NAME",
    "HandlerWorkLedgerDeclaredBrokerEmit",
    "declared_kafka_bus",
]
