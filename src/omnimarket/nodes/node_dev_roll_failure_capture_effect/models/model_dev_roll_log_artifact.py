# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One log artifact written by the incident handler."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelDevRollLogArtifact(BaseModel):
    """The result of writing one log capture to the artifact store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    container: str
    artifact_key: str | None
    bytes_total: int
    bytes_kept: int
    bytes_written: int
    sha256: str
    source_complete: bool
    written: bool
    complete: bool
    truncated: bool
    error: str | None
