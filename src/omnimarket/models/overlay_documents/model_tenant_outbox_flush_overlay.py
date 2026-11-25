# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay document `node_tenant_outbox_flush_effect/overlay.yaml`: deployment values, none in code.

Nothing here has a default. The database address is never in the overlay (``dsn_secret`` is the logical name the secret
store resolves), and the broker address is never in it either: ``event_bus`` names the lane whose declared broker the
publisher dials.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus


class ModelOutboxDatabaseOverlay(BaseModel):
    """Where the identity database's DSN is found, and the pool's bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dsn_secret: str = Field(min_length=1)
    secret_resolver_config_path: str = Field(min_length=1)
    max_pool_size: int = Field(ge=1)
    query_timeout_seconds: float = Field(gt=0)


class ModelTenantOutboxFlushOverlay(BaseModel):
    """The whole overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    batch_limit: int = Field(ge=1, le=1000)
    database: ModelOutboxDatabaseOverlay
    event_bus: ModelNodeEventBus


__all__: list[str] = ["ModelOutboxDatabaseOverlay", "ModelTenantOutboxFlushOverlay"]
