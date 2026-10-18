# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The single alert for one tenant's key pool."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_key_pool_expiry_alert_compute.models.model_key_pool_alert_key import (
    ModelKeyPoolAlertKey,
)
from omnimarket.nodes.node_key_pool_expiry_alert_compute.models.model_key_pool_expiry_slack_command import (
    ModelKeyPoolExpirySlackCommand,
)


class ModelKeyPoolExpiryAlert(BaseModel):
    """Every live key expiring inside the window, plus the pool's undated keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    window_days: int
    expiring: tuple[ModelKeyPoolAlertKey, ...] = Field(..., min_length=1)
    undated: tuple[ModelKeyPoolAlertKey, ...] = ()
    slack_command: ModelKeyPoolExpirySlackCommand


__all__ = ["ModelKeyPoolExpiryAlert"]
