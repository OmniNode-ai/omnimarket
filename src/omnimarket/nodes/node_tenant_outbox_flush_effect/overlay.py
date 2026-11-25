# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outbox flush overlay: every deployment fact the node needs, found by reference.

Found at ``<root>/node_tenant_outbox_flush_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``; the
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default. The database address is never
in the overlay (``dsn_secret`` is the logical name the secret store resolves), and the broker address is never in it
either: ``event_bus`` names the lane whose declared broker the publisher dials.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.overlay_documents.model_tenant_outbox_flush_overlay import (
    ModelOutboxDatabaseOverlay,
    ModelTenantOutboxFlushOverlay,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.handler_tenant_outbox_flush import (
    HandlerTenantOutboxFlush,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.tenant_event_kafka_publisher import (
    KafkaTenantEventPublisher,
    ProtocolKafkaProducer,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.tenant_outbox_postgres import (
    TenantOutboxPostgres,
    TenantOutboxStoreUnavailableError,
)

NODE_NAME = "node_tenant_outbox_flush_effect"


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelTenantOutboxFlushOverlay:
    """The overlay document, validated."""
    return load_structured_overlay(NODE_NAME, ModelTenantOutboxFlushOverlay, environ)


async def _build_producer() -> ProtocolKafkaProducer:
    """The producer for the broker this node's overlay ``event_bus`` declares; the SASL password stays in the object."""
    from aiokafka import AIOKafkaProducer

    from omnimarket.handlers.bus_binding.node_broker import resolve_dialable_broker

    broker = resolve_dialable_broker(NODE_NAME)
    options: dict[str, str] = {}
    if broker.sasl_mechanism is not None:
        options["sasl_mechanism"] = broker.sasl_mechanism
    if broker.sasl_username is not None:
        options["sasl_plain_username"] = broker.sasl_username
    if broker.sasl_password is not None:
        options["sasl_plain_password"] = broker.sasl_password.get_secret_value()
    producer: ProtocolKafkaProducer = AIOKafkaProducer(
        bootstrap_servers=broker.bootstrap_servers,
        security_protocol=broker.security_protocol or "PLAINTEXT",
        acks="all",
        **options,
    )
    return producer


def build_flush(
    overlay: ModelTenantOutboxFlushOverlay, *, topic: str
) -> tuple[HandlerTenantOutboxFlush, list[Callable[[], Awaitable[None]]]]:
    """The handler wired to the store and the bus the overlay describes, and what closes them. Connects to nothing."""
    database = overlay.database

    async def dsn() -> str:
        from omnimarket.inference.secret_store_resolver import (
            secret_store_from_config_path,
        )

        store = secret_store_from_config_path(database.secret_resolver_config_path)
        value = await store.get_secret(database.dsn_secret)
        if not value:
            raise TenantOutboxStoreUnavailableError("database secret has no value")
        return str(value)

    store = TenantOutboxPostgres(
        dsn_resolver=dsn,
        max_pool_size=database.max_pool_size,
        query_timeout_seconds=database.query_timeout_seconds,
    )
    publisher = KafkaTenantEventPublisher(_build_producer)
    handler = HandlerTenantOutboxFlush(store, publisher, topic=topic)
    return handler, [store.close, publisher.close]


__all__ = [
    "NODE_NAME",
    "ModelOutboxDatabaseOverlay",
    "ModelTenantOutboxFlushOverlay",
    "build_flush",
    "load_overlay",
]
