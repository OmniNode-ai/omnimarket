# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for node_env_sync_alert_effect."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelEnvSyncAlertResult(BaseModel):
    """Result of an env sync alert scan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alerts_created: int = Field(
        default=0,
        description="Number of Linear tickets created",
    )
    friction_events: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Friction YAML events emitted",
    )
