# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The manifest-recorded result, published on success (OMN-24013)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelDevRollManifestRecorded(BaseModel):
    """The manifest was recorded to the store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    issuer: str
    git_ref: str
    scope: str
    accepted_at: datetime
    record_ref: str
