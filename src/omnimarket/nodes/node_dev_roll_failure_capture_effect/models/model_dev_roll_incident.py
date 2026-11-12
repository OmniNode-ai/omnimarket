# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy agent's dev-roll incident event (OMN-24013, five whys R2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dev_roll_failure_capture_effect.models.model_dev_log_capture import (
    ModelDevLogCapture,
)


class ModelDevRollIncident(BaseModel):
    """The agent's dev-roll incident; the wire fields are spelled in the seam test."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    job_id: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    commit: str
    error: str
    project: str
    at: datetime
    restored_revision: str
    recovery_verified: bool
    recovery_skipped_healthy: bool | None
    recovery_error: str | None
    runtime_kept_revision: str | None
    health_probes: list[dict[str, Any]]
    log_captures: list[ModelDevLogCapture]
