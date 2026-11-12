# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The captured log artifact as the terminal record reports it (OMN-24013)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDevRollLogArtifact(BaseModel):
    """One captured log's store record, with a store error when the write failed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    container: str
    store_ref: str
    bytes_total: int = Field(ge=0)
    bytes_kept: int = Field(ge=0)
    complete: bool
    redacted: bool
    sha256: str = Field(min_length=64, max_length=64)
    agent_artifact_path: str
    store_error: str | None
