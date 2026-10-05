# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAuditTrailCompactSchedule: the daily producer of the compactor command.

The runtime tick arrives every ``tick_interval_ms``. The one tick whose clock
falls in ``[slot, slot + tick_interval)`` on a UTC day returns the compactor's
command; every other tick returns None, which the runtime treats as no output.
The decision reads only the tick, so the handler holds no state.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_audit_trail_compactor.models.model_audit_trail_input import (
    ModelCompactorCommand,
)

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"


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
        now = request.now.astimezone(dt.UTC)
        slot = now.replace(
            hour=self._cfg.run_hour_utc,
            minute=self._cfg.run_minute_utc,
            second=0,
            microsecond=0,
        )
        elapsed = now - slot
        if (
            dt.timedelta(0)
            <= elapsed
            < dt.timedelta(milliseconds=request.tick_interval_ms)
        ):
            return ModelCompactorCommand(
                lookback_days=self._cfg.lookback_days, dry_run=self._cfg.dry_run
            )
        return None


__all__: list[str] = [
    "HandlerAuditTrailCompactSchedule",
    "ModelAuditTrailCompactScheduleConfig",
    "schedule_config",
]
