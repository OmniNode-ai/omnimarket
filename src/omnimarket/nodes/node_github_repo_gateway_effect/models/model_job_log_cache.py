# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host-local completed job logs; policy can be supplied by a deployment overlay."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ModelJobLogCachePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root: Path | None = None
    ttl_seconds: int = Field(default=86400, gt=0)
    #: A lane's ``job-log`` read is cache-only; the filler node fills the cache (OMN-23032).
    lane_reads_cache_only: bool = True
    #: On-demand fills a lane read may spend per window; 0 means a lane miss is refused as today.
    lane_miss_fill_max: int = Field(default=0, ge=0)
    #: Window, in seconds, over which a lane's on-demand fills are counted.
    lane_miss_fill_window_seconds: int = Field(default=3600, gt=0)


class ModelCachedJobLog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    job_id: int
    total_bytes: int = Field(ge=0)
    sha256: str
    headers: dict[str, str] = Field(default_factory=dict)
