# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inbound alert event model — mirrors onex.evt.monitor.alert-detected.v1 payload."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAlertEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    alert_id: str = Field(..., description="UUID for this alert occurrence")
    source: str = Field(
        ..., description="Container or service that triggered the alert"
    )
    severity: str = Field(..., description="ERROR | CRITICAL | WARNING")
    pattern_matched: str = Field(..., description="Label of the pattern that fired")
    container: str = Field(..., description="Docker container name")
    exit_code: int | None = Field(default=None)
    restart_count: int | None = Field(default=None)
    full_message_text: str = Field(
        ..., description="Full log excerpt that triggered the alert"
    )
    raw_log_excerpt: str = Field(
        default="", description="Truncated raw log (<=500 chars)"
    )
    detected_at: str = Field(..., description="ISO8601 timestamp of detection")
    host: str = Field(..., description="Hostname of the monitor process")
