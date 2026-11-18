# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared configuration and the overlay-backed dependencies of the mount.

Nothing here connects to a database or a broker, or resolves a secret, until a
request needs it. The node's overlay document supplies every deployment value; an
absent or broken document leaves the gateway unconfigured and every request is
answered 503, never served unauthenticated.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import yaml

from omnimarket.handlers.edge_bearer_jwt_identity import (
    BearerJwtTenantIdentity,
    JwksSigningKeyProvider,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.adapters_memory import (
    InProcessTokenBucketLimiter,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.caller_identity import (
    CompositeCallerIdentity,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_contracts_read import (
    HandlerWorkflowContractsRead,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_receipt_read import (
    HandlerWorkflowReceiptRead,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_status_read import (
    HandlerWorkflowStatusRead,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_submit import (
    HandlerWorkflowSubmit,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.identity_api_key_postgres import (
    PostgresApiKeyTenantIdentity,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.publisher_bus import (
    KafkaWorkflowPublisher,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.store_postgres import (
    PostgresWorkflowStore,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_catalog import (
    WorkflowCatalog,
    WorkflowCatalogError,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_postgres_pool import (
    WorkflowPostgresPool,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_gateway_overlay import (
    ModelWorkflowGatewayOverlay,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_handler_settings import (
    ModelWorkflowSubmitSettings,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http_route import (
    ModelWorkflowHttpRoute,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)

logger = logging.getLogger(__name__)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_ID = "node_workflow_gateway_effect"


def _contract() -> dict[str, Any]:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("contract.yaml must be a mapping")
    return raw


def load_routes() -> tuple[ModelWorkflowHttpRoute, ...]:
    """The routes this mount serves, read from the contract's ``http_routes``."""
    return tuple(
        ModelWorkflowHttpRoute.model_validate(entry)
        for entry in _contract()["http_routes"]
    )


def load_contract_config() -> dict[str, Any]:
    """Non-secret settings from the contract's ``config`` block."""
    config = _contract()["config"]
    if not isinstance(config, dict):
        raise ValueError("contract config must be a mapping")
    return config


@dataclass(frozen=True)
class WorkflowGatewayDependencies:
    """Everything an endpoint needs once the gateway is configured."""

    identity: ProtocolHttpTenantIdentity
    api_key_header: str
    submit: HandlerWorkflowSubmit
    status: HandlerWorkflowStatusRead
    receipt: HandlerWorkflowReceiptRead
    contracts: HandlerWorkflowContractsRead
    max_body_bytes: int
    closers: tuple[Callable[[], Awaitable[None]], ...] = ()


class WorkflowGatewayDependencyProvider(Protocol):
    """Supply the dependencies per request; ``None`` means not configured."""

    async def get(self) -> WorkflowGatewayDependencies | None: ...

    async def close(self) -> None: ...


class StaticWorkflowGatewayDependencies:
    """Fixed dependencies, for the in-memory profile and tests."""

    def __init__(self, deps: WorkflowGatewayDependencies) -> None:
        self._deps = deps

    async def get(self) -> WorkflowGatewayDependencies | None:
        return self._deps

    async def close(self) -> None:
        for closer in self._deps.closers:
            await closer()


class OverlayWorkflowGatewayDependencies:
    """Build the dependencies once from the node's overlay document.

    A missing document is not cached, so one that appears later is used without a
    restart; a broken document or catalog is logged once by class name and is also
    not cached.
    """

    def __init__(self) -> None:
        self._deps: WorkflowGatewayDependencies | None = None

    async def get(self) -> WorkflowGatewayDependencies | None:
        if self._deps is not None:
            return self._deps
        try:
            overlay = load_structured_overlay(OVERLAY_ID, ModelWorkflowGatewayOverlay)
        except NodeOverlayNotFoundError:
            return None
        except NodeOverlayError as exc:
            logger.warning(
                "workflow gateway overlay unavailable (%s)", type(exc).__name__
            )
            return None
        try:
            catalog = WorkflowCatalog(overlay.catalog)
        except WorkflowCatalogError as exc:
            logger.warning("workflow gateway catalog refused (%s)", type(exc).__name__)
            return None
        self._deps = self._build(overlay, catalog)
        return self._deps

    @staticmethod
    def _build(
        overlay: ModelWorkflowGatewayOverlay, catalog: WorkflowCatalog
    ) -> WorkflowGatewayDependencies:
        config = load_contract_config()
        database = overlay.database
        pool = WorkflowPostgresPool(
            secret_resolver_config_path=database.secret_resolver_config_path,
            dsn_secret=database.dsn_secret,
            max_size=database.pool_max_size,
            query_timeout_seconds=database.query_timeout_seconds,
        )
        store = PostgresWorkflowStore(
            pool=pool,
            metric_key=overlay.quota.metric_key,
            window_key=overlay.quota.window_key,
            lock_namespace=overlay.quota.lock_namespace,
            rpm_metric_key=overlay.quota.rpm_metric_key,
            rpm_window_key=overlay.quota.rpm_window_key,
            clock=lambda: datetime.now(UTC),
        )
        publisher = KafkaWorkflowPublisher(
            bootstrap_servers_env=config["broker"]["bootstrap_servers_env"],
            timeout_seconds=overlay.publish_timeout_seconds,
        )
        identity = CompositeCallerIdentity(
            bearer=BearerJwtTenantIdentity(
                overlay.identity, JwksSigningKeyProvider(overlay.identity)
            ),
            api_key=PostgresApiKeyTenantIdentity(
                pool=pool, header=overlay.api_key.header, prefix=overlay.api_key.prefix
            ),
            api_key_header=overlay.api_key.header,
        )
        settings = ModelWorkflowSubmitSettings(
            gateway_instance_id=overlay.gateway_instance_id,
            envelope_schema_version=overlay.envelope_schema_version,
            default_refill_per_minute=overlay.rate_limit.refill_per_minute,
            default_burst_capacity=overlay.rate_limit.burst_capacity,
        )
        return WorkflowGatewayDependencies(
            identity=identity,
            api_key_header=overlay.api_key.header,
            submit=HandlerWorkflowSubmit(
                catalog=catalog,
                store=store,
                publisher=publisher,
                rate_limiter=InProcessTokenBucketLimiter(time.monotonic),
                settings=settings,
                clock=lambda: datetime.now(UTC),
                new_uuid=uuid4,
            ),
            status=HandlerWorkflowStatusRead(store=store),
            receipt=HandlerWorkflowReceiptRead(store=store),
            contracts=HandlerWorkflowContractsRead(catalog=catalog),
            max_body_bytes=int(config["http"]["max_body_bytes"]),
            closers=(publisher.close, pool.close),
        )

    async def close(self) -> None:
        deps, self._deps = self._deps, None
        if deps is None:
            return
        for closer in deps.closers:
            await closer()


__all__: list[str] = [
    "CONTRACT_PATH",
    "OVERLAY_ID",
    "OverlayWorkflowGatewayDependencies",
    "StaticWorkflowGatewayDependencies",
    "WorkflowGatewayDependencies",
    "WorkflowGatewayDependencyProvider",
    "load_contract_config",
    "load_routes",
]
