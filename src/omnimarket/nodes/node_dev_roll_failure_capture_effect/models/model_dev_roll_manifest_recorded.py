# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The output of the manifest handler."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelDevRollManifestRecorded(BaseModel):
    """Whether the manifest was recorded and where."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str | None
    issuer: str | None
    recorded: bool
    artifact_key: str | None
    error: str | None
