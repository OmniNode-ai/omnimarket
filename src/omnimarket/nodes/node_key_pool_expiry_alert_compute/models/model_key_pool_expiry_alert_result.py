# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result of one key pool expiry evaluation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_key_pool_expiry_alert_compute.models.model_key_pool_expiry_alert import (
    ModelKeyPoolExpiryAlert,
)


class ModelKeyPoolExpiryAlertResult(BaseModel):
    """``alert`` is None when no live key expires inside the window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    alert: ModelKeyPoolExpiryAlert | None = None


__all__ = ["ModelKeyPoolExpiryAlertResult"]
