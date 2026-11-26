# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant provision overlay: every deployment fact the node needs, found by reference.

Found at ``<root>/node_tenant_provision_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``; the
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: a lane, a realm, a secret name
or a bound the overlay leaves out is refused. The database address is never in the overlay: ``dsn_secret`` is the logical
name the secret store resolves (``tenant_identity.database.dsn``), and no value is held here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.bus_binding.node_broker import resolve_dialable_broker
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    TenantIdentityPostgres,
    TenantStoreUnavailableError,
)

NODE_NAME = "node_tenant_provision_effect"


class ModelTenantIdentityDatabaseOverlay(BaseModel):
    """Where the identity database's DSN is found, and the pool's bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dsn_secret: str = Field(min_length=1)
    secret_resolver_config_path: str = Field(min_length=1)
    max_pool_size: int = Field(ge=1)
    query_timeout_seconds: float = Field(gt=0)


class ModelTenantProvisionOverlay(BaseModel):
    """The whole overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    realms: tuple[str, ...]
    database: ModelTenantIdentityDatabaseOverlay
    # The bus the outbox flush publishes to: the lane only, the broker address stays in the lane declaration table.
    event_bus: ModelNodeEventBus | None = None


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelTenantProvisionOverlay:
    """The overlay document, validated."""
    return load_structured_overlay(NODE_NAME, ModelTenantProvisionOverlay, environ)


def build_store(overlay: ModelTenantProvisionOverlay) -> TenantIdentityPostgres:
    """The store the overlay describes."""
    database = overlay.database

    async def dsn() -> str:
        from omnimarket.inference.secret_store_resolver import (
            secret_store_from_config_path,
        )

        store = secret_store_from_config_path(database.secret_resolver_config_path)
        value = await store.get_secret(database.dsn_secret)
        if not value:
            raise TenantStoreUnavailableError("database secret has no value")
        return str(value)

    return TenantIdentityPostgres(
        dsn_resolver=dsn,
        max_pool_size=database.max_pool_size,
        query_timeout_seconds=database.query_timeout_seconds,
    )


def build_publisher(
    overlay: ModelTenantProvisionOverlay,
) -> Callable[[str, bytes], Awaitable[None]]:
    """The publisher the flush sends each outbox envelope with: one produce on the lane the overlay's ``event_bus`` names."""
    if overlay.event_bus is None:
        raise NodeOverlayError(
            f"{NODE_NAME}: the overlay declares no event_bus, so the outbox flush has no bus to publish to"
        )

    async def publish(topic: str, payload: bytes) -> None:
        from aiokafka import AIOKafkaProducer

        from omnimarket.topic_namespace import apply_topic_namespace

        broker = resolve_dialable_broker(NODE_NAME)
        kwargs: dict[str, str] = {}
        if broker.sasl_mechanism is not None:
            kwargs["sasl_mechanism"] = broker.sasl_mechanism
        if broker.sasl_username is not None:
            kwargs["sasl_plain_username"] = broker.sasl_username
        if broker.sasl_password is not None:
            kwargs["sasl_plain_password"] = broker.sasl_password.get_secret_value()
        producer = AIOKafkaProducer(
            bootstrap_servers=broker.bootstrap_servers,
            security_protocol=broker.security_protocol or "PLAINTEXT",
            **kwargs,
        )
        await producer.start()
        try:
            await producer.send_and_wait(apply_topic_namespace(topic), payload)
        finally:
            await producer.stop()

    return publish


__all__ = [
    "NODE_NAME",
    "ModelTenantIdentityDatabaseOverlay",
    "ModelTenantProvisionOverlay",
    "build_publisher",
    "build_store",
    "load_overlay",
]
