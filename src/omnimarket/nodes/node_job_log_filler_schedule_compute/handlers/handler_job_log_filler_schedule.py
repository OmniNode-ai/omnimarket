# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerJobLogFillerSchedule: the runtime tick asks the job-log filler for a pass (OMN-23113).

Once per ``config.job_log_filler.schedule.interval_seconds`` the one tick that opens a
window returns a :class:`ModelJobLogFillRequest`; every other tick returns None, which
the runtime treats as no output. The decision reads only the tick, so the handler holds
no state and a restart cannot repeat a window. The window rule is the shared one of
``omnimarket.models.model_runtime_tick_schedule``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_runtime_tick_schedule import (
    ModelRuntimeTickScheduleConfig,
    scheduled_fire,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_job_log_filler import (
    ModelJobLogFillRequest,
)

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"


class ModelJobLogFillerScheduleConfig(BaseModel):
    """The ``config.job_log_filler`` block: the interval and the pass's narrowing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule: ModelRuntimeTickScheduleConfig
    profile: str | None = Field(default=None, min_length=1)
    repos: tuple[str, ...] = ()
    max_reads: int | None = Field(default=None, ge=1)


@lru_cache(maxsize=1)
def schedule_config() -> ModelJobLogFillerScheduleConfig:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelJobLogFillerScheduleConfig.model_validate(
        data["config"]["job_log_filler"]
    )


class HandlerJobLogFillerSchedule:
    """COMPUTE handler: tick in, one fill request out once per interval."""

    def __init__(self, config: ModelJobLogFillerScheduleConfig | None = None) -> None:
        self._cfg = config or schedule_config()

    def handle(self, request: ModelRuntimeTick) -> ModelJobLogFillRequest | None:
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
        return ModelJobLogFillRequest(
            correlation_id=uuid5(NAMESPACE_URL, fire.fire_id),
            profile=self._cfg.profile,
            repos=self._cfg.repos,
            max_reads=self._cfg.max_reads,
        )


__all__: list[str] = [
    "HandlerJobLogFillerSchedule",
    "ModelJobLogFillerScheduleConfig",
    "schedule_config",
]
