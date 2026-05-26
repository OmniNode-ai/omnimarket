# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_env_sync_alert_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelEnvSyncAlertRequest(BaseModel):
    """Request to scan logs and emit env sync drift alerts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    log_paths: list[str] = Field(
        description="Filesystem paths to runtime log files to scan for sync drift"
    )
    alert_threshold: int = Field(
        default=1,
        description="Minimum number of drift occurrences before raising an alert",
    )
