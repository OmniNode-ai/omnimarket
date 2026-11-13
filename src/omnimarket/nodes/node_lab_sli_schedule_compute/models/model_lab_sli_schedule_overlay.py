# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployment overlay the lab SLI schedule reads: the interval, the host and the source paths."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_runtime_tick_schedule import ModelRuntimeTickScheduleConfig


class ModelLabSliScheduleOverlay(BaseModel):
    """The deployment facts the lab SLI schedule needs; none has a packaged default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule: ModelRuntimeTickScheduleConfig
    collector_host: str = Field(min_length=1)
    pr_watcher_state_path: str
    disk_paths: tuple[str, ...]


__all__ = ["ModelLabSliScheduleOverlay"]
