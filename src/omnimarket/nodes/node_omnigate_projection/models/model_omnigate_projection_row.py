# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection models for OmniGate dashboard snapshots."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelOmniGateProjectionRow(BaseModel):
    """One OmniGate activity row for projection snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_id: str
    project_name: str
    branch: str = ""
    base_sha: str = ""
    head_sha: str = ""
    diff_hash: str | None = None
    config_hash: str | None = None
    status: str
    action: str | None = None
    reason: str = ""
    total_checks: int = Field(default=0, ge=0)
    failed_checks: int = Field(default=0, ge=0)
    advisory_checks: int = Field(default=0, ge=0)
    pending_checks: int = Field(default=0, ge=0)
    observed_at: datetime


class ModelOmniGateMetricsSnapshot(BaseModel):
    """Aggregate OmniGate metrics snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_events: int = Field(default=0, ge=0)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    advisory: int = Field(default=0, ge=0)
    pending: int = Field(default=0, ge=0)


__all__ = ["ModelOmniGateMetricsSnapshot", "ModelOmniGateProjectionRow"]
