# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tolerant input of the manifest topic, as this node reads it."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelDevRollManifestEvent(BaseModel):
    """The deploy agent's manifest fields; every field optional."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: str | None = None
    job_id: str | None = None
    issuer: str | None = None
    git_ref: str | None = None
    scope: str | None = None
    requested_at: datetime | None = None
    accepted_at: datetime | None = None
    open_pr: str | None = None
    rollback: bool | None = None
