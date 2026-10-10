# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAuditTrailCompactSchedule: the daily producer of the compactor command.

The runtime tick arrives every ``tick_interval_ms``. The one tick whose clock
falls in ``[slot, slot + tick_interval + lateness)`` on a UTC day returns the
compactor's command; every other tick returns None, which the runtime treats as
no output. The decision reads only the tick, so the handler holds no state. The
daily slot is the one-day interval of the shared runtime-tick gate, so a tick
that ran late past its ``scheduled_at`` still opens the slot (OMN-20867).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_audit_trail_compactor_command import ModelCompactorCommand
from omnimarket.models.model_runtime_tick_schedule import (
    ModelRuntimeTickScheduleConfig,
)

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"
_DAY_SECONDS = 86400


class ModelAuditTrailCompactScheduleConfig(BaseModel):
    """The ``config.audit_trail_compact_schedule`` block of the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_hour_utc: int = Field(..., ge=0, le=23)
    run_minute_utc: int = Field(..., ge=0, le=59)
    lookback_days: int = Field(..., ge=1)
    dry_run: bool


@lru_cache(maxsize=1)
def schedule_config() -> ModelAuditTrailCompactScheduleConfig:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelAuditTrailCompactScheduleConfig.model_validate(
        data["config"]["audit_trail_compact_schedule"]
    )


class HandlerAuditTrailCompactSchedule:
    """COMPUTE handler: tick in, compactor command out on the daily slot."""

    def __init__(
        self, config: ModelAuditTrailCompactScheduleConfig | None = None
    ) -> None:
        self._cfg = config or schedule_config()

    def handle(self, request: ModelRuntimeTick) -> ModelCompactorCommand | None:
        daily = ModelRuntimeTickScheduleConfig(
            workflow="audit-trail-compact",
            interval_seconds=_DAY_SECONDS,
            offset_seconds=self._cfg.run_hour_utc * 3600
            + self._cfg.run_minute_utc * 60,
        )
        if (
            daily.due_window(
                request.now,
                request.tick_interval_ms,
                scheduled_at=request.scheduled_at,
            )
            is None
        ):
            return None
        return ModelCompactorCommand(
            lookback_days=self._cfg.lookback_days, dry_run=self._cfg.dry_run
        )


__all__: list[str] = [
    "HandlerAuditTrailCompactSchedule",
    "ModelAuditTrailCompactScheduleConfig",
    "schedule_config",
]
