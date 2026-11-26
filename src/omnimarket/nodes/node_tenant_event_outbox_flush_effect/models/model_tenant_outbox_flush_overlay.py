# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployment document ``node_tenant_event_outbox_flush_effect/overlay.yaml``; nothing in it is a secret."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTenantOutboxFlushDatabase(BaseModel):
    """Where the identity database's DSN is found (the logical secret the provision node uses) and the pool's bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dsn_secret: str = Field(min_length=1)
    secret_resolver_config_path: str = Field(min_length=1)
    pool_max_size: int = Field(default=2, gt=0)
    query_timeout_seconds: float = Field(default=10.0, gt=0)


class ModelTenantOutboxFlushOverlay(BaseModel):
    """Every deployment value of the flush."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    database: ModelTenantOutboxFlushDatabase
    broker_bootstrap_servers_env: str = Field(
        default="KAFKA_BOOTSTRAP_SERVERS", min_length=1
    )
    publish_timeout_seconds: float = Field(default=10.0, gt=0)


__all__ = ["ModelTenantOutboxFlushDatabase", "ModelTenantOutboxFlushOverlay"]
