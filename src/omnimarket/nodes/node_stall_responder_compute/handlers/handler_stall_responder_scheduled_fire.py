# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerStallResponderScheduledFire: the runtime tick decides when the stall responder is due (OMN-26438).

The tick route of node_stall_responder_compute. The one tick that opens an interval window of
``config.stall_responder.schedule`` returns that window's fire, which the runtime publishes on the
node's terminal topic; every other tick returns None, which the runtime treats as no output. The
decision reads only the tick, so the handler holds no state and stays pure (omnimarket.models
.model_runtime_tick_schedule).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.model_runtime_tick_schedule import (
    ModelRuntimeTickScheduleConfig,
    ModelScheduledFire,
    scheduled_fire,
)

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"


@lru_cache(maxsize=1)
def schedule_config() -> ModelRuntimeTickScheduleConfig:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelRuntimeTickScheduleConfig.model_validate(
        data["config"]["stall_responder"]["schedule"]
    )


class HandlerStallResponderScheduledFire:
    """COMPUTE handler: tick in, the due window's fire out, nothing for every other tick."""

    def __init__(self, config: ModelRuntimeTickScheduleConfig | None = None) -> None:
        self._cfg = config or schedule_config()

    def handle(self, request: ModelRuntimeTick) -> ModelScheduledFire | None:
        return scheduled_fire(
            self._cfg,
            now=request.now,
            scheduled_at=request.scheduled_at,
            tick_interval_ms=request.tick_interval_ms,
            tick_id=request.tick_id,
            scheduler_id=request.scheduler_id,
        )


__all__: list[str] = ["HandlerStallResponderScheduledFire", "schedule_config"]
