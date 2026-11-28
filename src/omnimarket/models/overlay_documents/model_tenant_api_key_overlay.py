# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay document `node_tenant_api_key_effect/overlay.yaml`: deployment values, none in code.

``lane`` is required. ``dev_secret_store_environment`` is set only in the dev overlay and read by the CLI's dev-secret
guard. ``max_lifetime_days`` unset means a key may be perpetual (dev); set, it bounds every key and refuses a null expiry.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus


class ModelTenantApiKeyOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    dev_secret_store_environment: str | None = Field(default=None, min_length=1)
    max_lifetime_days: int | None = Field(default=None, ge=1)
    event_bus: ModelNodeEventBus | None = None


__all__: list[str] = ["ModelTenantApiKeyOverlay"]
