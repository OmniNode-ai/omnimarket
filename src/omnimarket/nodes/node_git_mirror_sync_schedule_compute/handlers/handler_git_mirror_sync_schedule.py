# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerGitMirrorSyncSchedule: the runtime tick asks the git mirror for a detecting sync (OMN-19998).

Once per ``config.git_mirror_sync.schedule.interval_seconds`` the one tick that
opens a window returns a detecting sync request for one watched repository;
every other tick returns None, which the runtime treats as no output. The
watched repositories take turns: the window's index on the interval grid picks
the repository, so with N repositories each one syncs once every N windows. The
decision reads only the tick, so the handler holds no state and a restart
cannot repeat a window. The window rule is the shared one of
``omnimarket.models.model_runtime_tick_schedule``.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.git_mirror.model_git_mirror_sync_requested import (
    ModelGitMirrorSyncRequested,
)
from omnimarket.models.model_runtime_tick_schedule import (
    ModelRuntimeTickScheduleConfig,
    scheduled_fire,
)

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


class ModelGitMirrorSyncScheduleConfig(BaseModel):
    """The ``config.git_mirror_sync`` block: the interval and the watched repositories."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule: ModelRuntimeTickScheduleConfig
    repos: tuple[str, ...] = Field(..., min_length=1)


@lru_cache(maxsize=1)
def schedule_config() -> ModelGitMirrorSyncScheduleConfig:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelGitMirrorSyncScheduleConfig.model_validate(
        data["config"]["git_mirror_sync"]
    )


class HandlerGitMirrorSyncSchedule:
    """COMPUTE handler: tick in, one repository's detecting sync request out once per interval."""

    def __init__(self, config: ModelGitMirrorSyncScheduleConfig | None = None) -> None:
        self._cfg = config or schedule_config()

    def handle(self, request: ModelRuntimeTick) -> ModelGitMirrorSyncRequested | None:
        fire = scheduled_fire(
            self._cfg.schedule,
            now=request.now,
            scheduled_at=request.scheduled_at,
            tick_interval_ms=request.tick_interval_ms,
            tick_id=request.tick_id,
            scheduler_id=request.scheduler_id,
        )
        if fire is None:
            return None
        window_index = int(
            (fire.window_start - _EPOCH).total_seconds() // fire.interval_seconds
        )
        repo = self._cfg.repos[window_index % len(self._cfg.repos)]
        return ModelGitMirrorSyncRequested(
            repo=repo,
            correlation_id=uuid5(NAMESPACE_URL, f"{fire.fire_id}/{repo}"),
        )


__all__: list[str] = [
    "HandlerGitMirrorSyncSchedule",
    "ModelGitMirrorSyncScheduleConfig",
    "schedule_config",
]
