# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerMeteringSummaryRefreshSchedule: the backstop refresh of every tenant (OMN-17427).

Once per ``config.metering_summary_refresh.schedule.interval_seconds`` the one
runtime tick that opens the window returns a whole refresh of every tenant;
every other tick returns None, which the runtime treats as no output. A run
whose terminal refresh was lost, or read before its row was durable, is in its
tenant's rows by the next window. The first window after a deploy is the
backfill: every tenant's every day and all-time row.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.model_metering_summary_refresh_requested import (
    EnumMeteringRefreshReason,
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.models.model_runtime_tick_schedule import (
    ModelRuntimeTickScheduleConfig,
    scheduled_fire,
)

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"


@lru_cache(maxsize=1)
def schedule_config() -> ModelRuntimeTickScheduleConfig:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelRuntimeTickScheduleConfig.model_validate(
        data["config"]["metering_summary_refresh"]["schedule"]
    )


class HandlerMeteringSummaryRefreshSchedule:
    """COMPUTE handler: tick in, a whole refresh of every tenant out once per interval."""

    def __init__(self, config: ModelRuntimeTickScheduleConfig | None = None) -> None:
        self._cfg = config or schedule_config()

    def handle(
        self, request: ModelRuntimeTick
    ) -> ModelMeteringSummaryRefreshRequested | None:
        fire = scheduled_fire(
            self._cfg,
            now=request.now,
            scheduled_at=request.scheduled_at,
            tick_interval_ms=request.tick_interval_ms,
            tick_id=request.tick_id,
            scheduler_id=request.scheduler_id,
        )
        if fire is None:
            return None
        return ModelMeteringSummaryRefreshRequested(
            refresh_id=fire.fire_id,
            reason=EnumMeteringRefreshReason.SCHEDULE,
            tenant_ids=None,
            days=None,
            requested_at=fire.tick_now,
        )


__all__: list[str] = ["HandlerMeteringSummaryRefreshSchedule", "schedule_config"]
