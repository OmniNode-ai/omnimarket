# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding factories for node_work_ledger_emit_effect.

OMN-23452: the emitter the work ledger binds publishes through the broker the node's contract overlay
declares (``event_bus``, resolved by ``resolve_dialable_broker``), never ``KAFKA_BOOTSTRAP_SERVERS``. No
``KAFKA_*`` variable is read for the broker or its security protocol; the SASL identity a host holds only
supplies the credential of the lane the declaration already selected. The shared emit effect spools the
event first and asks its publish adapter for the target afterwards, so a missing declaration is raised
from the adapter at that point and costs the row nothing.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator
from typing import Any, Final

from omnimarket.handlers.bus_binding.node_broker import (
    NodeBrokerResolutionError,
    resolve_dialable_broker,
)
from omnimarket.handlers.network_bindings import network_hosts
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
    KafkaEventPublisher,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit_guard import (
    GUARD_NAME,
    HandlerWorkLedgerEmitGuard,
    LedgerTestWriteRefusedError,
)

NODE_NAME: Final[str] = "node_work_ledger_emit_effect"
SPOOL_ONLY_ENV: Final[str] = "ONEX_EMIT_EFFECT_SPOOL_ONLY"
SPOOL_ONLY_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def declared_kafka_bus(binding: ModelNodeBroker) -> Any:
    """The Kafka bus for a declared broker, built only from the declaration (no env override)."""
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

    secret = binding.sasl_password
    config = ModelKafkaEventBusConfig(
        bootstrap_servers=binding.bootstrap_servers,
        security_protocol=binding.security_protocol or "PLAINTEXT",
        sasl_mechanism=binding.sasl_mechanism,
        sasl_plain_username=binding.sasl_username,
        sasl_plain_password=secret.get_secret_value() if secret is not None else None,
    )
    return EventBusKafka(config)


def _refuse_test_process(servers: str) -> None:
    """A test process never dials a declared broker that is not loopback (the emit guard's rule, OMN-19513)."""
    signal = HandlerWorkLedgerEmitGuard.test_context()
    if signal is None:
        return
    loopback = {"", *network_hosts("loopback.hosts")}
    for server in servers.split(","):
        host = server.strip().rsplit(":", 1)[0].strip("[]").lower()
        if host not in loopback:
            raise LedgerTestWriteRefusedError(
                f"{GUARD_NAME} REFUSED -- a test process ({signal}) tried to publish to the real "
                f"work-ledger topics through the declared broker (host {host}). Nothing was published. "
                "A test declares a loopback lane and an in-memory bus (node_work_ledger_emit_effect, OMN-23452)."
            )


class DeclaredBrokerPublisher:
    """The emit effect's publish adapter for the broker this node's overlay declares.

    The broker is resolved when the adapter is first asked for its target or to publish, never when it is
    built, so the constructor does no I/O and the emit effect has spooled the event before a missing
    declaration is raised as ``NodeBrokerResolutionError``.
    """

    def __init__(
        self,
        *,
        broker: Callable[[str], ModelNodeBroker] = resolve_dialable_broker,
        bus_factory: Callable[[ModelNodeBroker], Any] | None = None,
    ) -> None:
        self._broker = broker
        self._bus_factory = bus_factory
        self._publisher_cache: KafkaEventPublisher | None = None
        self._identity: str | None = None

    def _publisher(self) -> KafkaEventPublisher:
        if self._publisher_cache is not None:
            return self._publisher_cache
        binding = self._broker(NODE_NAME)
        if binding.bootstrap_servers is None:
            raise NodeBrokerResolutionError(
                f"{NODE_NAME} resolved to a bus with no broker address (overlay {binding.overlay_path}); "
                "declare `event_bus: {type: kafka, lane: <lane id>}`."
            )
        _refuse_test_process(binding.bootstrap_servers)
        self._identity = (
            f"{binding.bootstrap_servers}|{binding.security_protocol or ''}|"
            f"{binding.sasl_username or ''}"
        )
        factory = self._bus_factory
        self._publisher_cache = KafkaEventPublisher(
            binding.bootstrap_servers,
            publish_identity=self._identity,
            bus_factory=lambda: (factory or declared_kafka_bus)(binding),
        )
        return self._publisher_cache

    @property
    def publish_identity(self) -> str | None:
        """The target and principal, without the credential; resolves the declaration."""
        self._publisher()
        return self._identity

    @contextlib.contextmanager
    def session(self, *, deadline: float | None = None) -> Iterator[None]:
        with self._publisher().session(deadline=deadline):
            yield

    def publish(
        self,
        topic: str,
        payload: Any,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._publisher().publish(
            topic,
            payload,
            key=key,
            correlation_id=correlation_id,
            content_event_id=content_event_id,
            timeout_seconds=timeout_seconds,
        )


def create_handler_event_emit_effect() -> HandlerEventEmitEffect:
    """Bind the event emitter used by the work ledger: the declared broker, or spool-only when declared so."""
    if os.environ.get(SPOOL_ONLY_ENV, "").strip().lower() in SPOOL_ONLY_TRUE:
        return HandlerEventEmitEffect()
    return HandlerEventEmitEffect(publish_adapter=DeclaredBrokerPublisher())


__all__ = [
    "NODE_NAME",
    "DeclaredBrokerPublisher",
    "create_handler_event_emit_effect",
    "declared_kafka_bus",
]
