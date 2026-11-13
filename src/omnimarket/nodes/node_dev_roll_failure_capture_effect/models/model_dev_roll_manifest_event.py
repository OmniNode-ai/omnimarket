# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tolerant input of the manifest topic."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelDevRollManifestEvent(BaseModel):
    """The producer's manifest fields; every field optional."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: str | None = None
    job_id: str | None = None
    issuer: str | None = None
    git_ref: str | None = None
    scope: str | None = None
    requested_at: str | None = None
    accepted_at: str | None = None
    open_pr: str | None = None
    rollback: bool | None = None
