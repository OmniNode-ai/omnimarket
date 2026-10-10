# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabJobCheckSchedule: the runtime tick decides when a lab job sweep is due (OMN-20604).

The runtime tick arrives every ``tick_interval_ms``. Once per
``config.lab_job_check.schedule.interval_seconds`` (the plan's
``check_interval_s``, default 300) the one tick whose clock falls in
``[window_start, window_start + tick_interval)`` returns
``lab-job-check-requested{scope: nonterminal}``; every other tick returns None,
which the runtime treats as no output. The decision reads only the tick, so the
handler holds no state and a restart cannot repeat a window. The window rule is
the shared one of ``omnimarket.models.model_runtime_tick_schedule``, the
stateless slot idiom of node_audit_trail_compact_schedule_compute.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.enums.enum_lab_job_check import EnumLabJobCheckScope
from omnimarket.models.lab_job.model_lab_job_check import ModelLabJobCheckRequested
from omnimarket.models.model_runtime_tick_schedule import (
    ModelRuntimeTickScheduleConfig,
    scheduled_fire,
)

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"


@lru_cache(maxsize=1)
def schedule_config() -> ModelRuntimeTickScheduleConfig:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelRuntimeTickScheduleConfig.model_validate(
        data["config"]["lab_job_check"]["schedule"]
    )


class HandlerLabJobCheckSchedule:
    """COMPUTE handler: tick in, a nonterminal-scope sweep request out once per interval."""

    def __init__(self, config: ModelRuntimeTickScheduleConfig | None = None) -> None:
        self._cfg = config or schedule_config()

    def handle(self, request: ModelRuntimeTick) -> ModelLabJobCheckRequested | None:
        fire = scheduled_fire(
            self._cfg,
            now=request.now,
            tick_interval_ms=request.tick_interval_ms,
            tick_id=request.tick_id,
            scheduler_id=request.scheduler_id,
        )
        if fire is None:
            return None
        return ModelLabJobCheckRequested(
            check_id=fire.fire_id,
            scope=EnumLabJobCheckScope.NONTERMINAL,
            requested_at=fire.tick_now,
        )


__all__: list[str] = ["HandlerLabJobCheckSchedule", "schedule_config"]
