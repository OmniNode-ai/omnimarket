# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tolerant input of the incident topic."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, JsonValue


class ModelDevRollIncidentEvent(BaseModel):
    """The producer's incident fields; the handler decides what is unreadable."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: str | None = None
    job_id: str | None = None
    issuer: str | None = None
    commit: str | None = None
    error: str | None = None
    project: str | None = None
    at: str | None = None
    restored_revision: str | None = None
    recovery_verified: bool | None = None
    recovery_skipped_healthy: bool | None = None
    recovery_error: str | None = None
    runtime_kept_revision: str | None = None
    health_probes: list[dict[str, JsonValue]] = []
    log_captures: list[dict[str, JsonValue]] = []
