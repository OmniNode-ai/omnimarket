# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding factories for node_agent_message_effect.

Both operations dial the broker this node's contract overlay declares (``event_bus``, resolved by
``resolve_dialable_broker``), never ``KAFKA_BOOTSTRAP_SERVERS``. The publish adapter and the Kafka bus
construction are the work-ledger emit node's, called with this node's name; a node with no declaration
raises ``NodeBrokerResolutionError``, which the handler answers as a typed ``broker_not_declared``.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any, Final

from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.handlers.bus_binding.node_broker import resolve_dialable_broker
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_agent_message_effect.adapters.adapter_settings_store_agent_directory import (
    SettingsStoreAgentDirectory,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_work_ledger_emit_effect.container.container_work_ledger_emit_effect import (
    SPOOL_ONLY_ENV,
    SPOOL_ONLY_TRUE,
    DeclaredBrokerPublisher,
    declared_kafka_bus,
)

NODE_NAME: Final[str] = "node_agent_message_effect"


def resolve_declared_broker(_caller: str = NODE_NAME) -> ModelNodeBroker:
    """This node's declared broker, whatever node name the shared publisher passes."""
    return resolve_dialable_broker(NODE_NAME)


def create_directory() -> SettingsStoreAgentDirectory:
    """The sender registry, signing keys and cutover flag, read from the settings store the host declares."""
    from omnimarket.handlers.delegation_scenario_settings import (
        SecretStoreSettingsReader,
    )
    from omnimarket.nodes.node_agent_message_effect.handlers.handler_agent_message import (
        load_contract,
    )

    layout = load_contract()["message_contract"]["settings_store"]
    return SettingsStoreAgentDirectory(SecretStoreSettingsReader(), layout)


def create_emitter() -> HandlerEventEmitEffect:
    """The emit effect bound to the declared broker, or spool-only when the host declares so."""
    if os.environ.get(SPOOL_ONLY_ENV, "").strip().lower() in SPOOL_ONLY_TRUE:
        return HandlerEventEmitEffect()
    return HandlerEventEmitEffect(
        publish_adapter=DeclaredBrokerPublisher(broker=resolve_declared_broker)
    )


class DeclaredKafkaReadBus:
    """The read side: a Kafka bus built only from the declaration, started on first use, closed by the handler."""

    def __init__(self, bus: Any) -> None:
        self._bus = bus
        self._started = False

    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
        group_id: str,
        auto_offset_reset: str,
    ) -> Callable[[], Awaitable[None]]:
        if not self._started:
            await self._bus.start()
            self._started = True
        stop: Callable[[], Awaitable[None]] = await self._bus.subscribe(
            topic,
            on_message=on_message,
            group_id=group_id,
            auto_offset_reset=auto_offset_reset,
        )
        return stop

    async def close(self) -> None:
        if self._started:
            await self._bus.close()


def create_bus() -> DeclaredKafkaReadBus:
    """The read bus for the declared broker; raises ``NodeBrokerResolutionError`` when none is declared."""
    return DeclaredKafkaReadBus(declared_kafka_bus(resolve_declared_broker()))


__all__: list[str] = [
    "NODE_NAME",
    "DeclaredKafkaReadBus",
    "create_bus",
    "create_directory",
    "create_emitter",
    "resolve_declared_broker",
]
