# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the flush's ports from the node's overlay document; nothing environment-specific is in code."""

from __future__ import annotations

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_tenant_event_outbox_flush_effect.handlers.publisher_kafka import (
    KafkaTenantEventPublisher,
)
from omnimarket.nodes.node_tenant_event_outbox_flush_effect.handlers.store_postgres import (
    PostgresTenantOutboxStore,
)
from omnimarket.nodes.node_tenant_event_outbox_flush_effect.models.model_tenant_outbox_flush_overlay import (
    ModelTenantOutboxFlushOverlay,
)

OVERLAY_ID = "node_tenant_event_outbox_flush_effect"


def build_ports_from_overlay() -> tuple[
    PostgresTenantOutboxStore, KafkaTenantEventPublisher
]:
    """Load overlay.yaml and return the Postgres store and the Kafka publisher."""
    overlay = load_structured_overlay(OVERLAY_ID, ModelTenantOutboxFlushOverlay)
    database = overlay.database
    return (
        PostgresTenantOutboxStore(
            secret_resolver_config_path=database.secret_resolver_config_path,
            dsn_secret=database.dsn_secret,
            max_size=database.pool_max_size,
            query_timeout_seconds=database.query_timeout_seconds,
        ),
        KafkaTenantEventPublisher(
            bootstrap_servers_env=overlay.broker_bootstrap_servers_env,
            timeout_seconds=overlay.publish_timeout_seconds,
        ),
    )


__all__ = ["OVERLAY_ID", "build_ports_from_overlay"]
