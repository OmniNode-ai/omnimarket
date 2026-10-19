# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One request to node_compact_package_read_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCompactPackageReadRequest(BaseModel):
    """A session transcript to read the newest compaction from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transcript_path: str = Field(min_length=1)
    tail_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    attempts: int = Field(default=6, ge=1)
    pause_s: float = Field(default=0.5, ge=0)


__all__ = ["ModelCompactPackageReadRequest"]
