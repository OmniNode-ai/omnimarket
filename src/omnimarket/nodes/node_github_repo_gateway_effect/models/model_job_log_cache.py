# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host-local completed job logs; policy can be supplied by a deployment overlay."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ModelJobLogCachePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root: Path | None = None
    ttl_seconds: int = Field(default=86400, gt=0)


class ModelCachedJobLog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    job_id: int
    total_bytes: int = Field(ge=0)
    sha256: str
    headers: dict[str, str] = Field(default_factory=dict)
