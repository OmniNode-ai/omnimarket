# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The collect request: which interval, which host, which sources to read."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelLabSliCollectRequest(BaseModel):
    """``onex.cmd.omnimarket.lab-sli-collect-requested.v1``: collect one interval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_start: datetime
    now: datetime
    collector_host: str = Field(min_length=1)
    # The PR watcher state file; empty skips the PR indicators (they read unknown).
    pr_watcher_state_path: str = ""
    # Paths whose filesystem free space is read; empty skips the disk indicator.
    disk_paths: tuple[str, ...] = ()


__all__ = ["ModelLabSliCollectRequest"]
