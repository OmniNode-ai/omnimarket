# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy agent's captured log for one container (OMN-24013, five whys R2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDevLogCapture(BaseModel):
    """One container's captured log, as the deploy agent publishes it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    container: str
    bytes_total: int = Field(ge=0)
    bytes_kept: int = Field(ge=0)
    complete: bool
    redacted: bool
    sha256: str = Field(min_length=64, max_length=64)
    artifact_path: str
    error: str | None
    log: str
