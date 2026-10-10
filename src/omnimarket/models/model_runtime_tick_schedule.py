# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Interval schedule on the platform runtime tick, shared by the tick-driven decision nodes.

The runtime tick arrives every ``tick_interval_ms``. A workflow that runs every
``interval_seconds`` (first fire ``offset_seconds`` past the epoch-aligned interval) fires on
the one tick whose clock falls in ``[window_start, window_start + tick_interval)``; every other
tick inside the interval produces nothing. The decision reads only the tick, so a COMPUTE
handler stays pure and holds no last-run state, and a restart cannot repeat a window: the
stateless-slot idiom of node_audit_trail_compact_schedule_compute, generalised from a daily
slot to an interval (OMN-20867).

Lab-fill, the hourly tick and the merge-throughput tick each consume these models, so they
live in omnimarket.models and in no node's models package.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


class ModelRuntimeTickScheduleConfig(BaseModel):
    """The ``config.<node>.schedule`` block of a tick-driven contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow: str = Field(..., min_length=1)
    interval_seconds: int = Field(..., ge=60)
    offset_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _offset_inside_interval(self) -> ModelRuntimeTickScheduleConfig:
        if self.offset_seconds >= self.interval_seconds:
            raise ValueError(
                f"offset_seconds ({self.offset_seconds}) must be below "
                f"interval_seconds ({self.interval_seconds})"
            )
        return self

    def due_window(self, now: dt.datetime, tick_interval_ms: int) -> dt.datetime | None:
        """The start of the window this tick opens, or None when the tick opens none."""
        if tick_interval_ms <= 0:
            return None
        now = now.astimezone(dt.UTC)
        since = now - _EPOCH - dt.timedelta(seconds=self.offset_seconds)
        interval = dt.timedelta(seconds=self.interval_seconds)
        start = now - (since % interval)
        if now - start < dt.timedelta(milliseconds=tick_interval_ms):
            return start.replace(microsecond=0)
        return None


class ModelScheduledFire(BaseModel):
    """The runtime tick decided that one interval's run of a workflow is due."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow: str
    fire_id: str = Field(
        ...,
        description="<workflow>-<window start, compact ISO-Z>; the same window always "
        "names the same fire, so a consumer dedupes a replayed tick on it.",
    )
    window_start: dt.datetime
    interval_seconds: int
    tick_id: UUID
    tick_now: dt.datetime
    scheduler_id: str


def scheduled_fire(
    config: ModelRuntimeTickScheduleConfig,
    *,
    now: dt.datetime,
    tick_interval_ms: int,
    tick_id: UUID,
    scheduler_id: str,
) -> ModelScheduledFire | None:
    """The fire this tick opens, or None for every other tick inside the interval."""
    start = config.due_window(now, tick_interval_ms)
    if start is None:
        return None
    return ModelScheduledFire(
        workflow=config.workflow,
        fire_id=f"{config.workflow}-{start:%Y%m%dT%H%M%SZ}",
        window_start=start,
        interval_seconds=config.interval_seconds,
        tick_id=tick_id,
        tick_now=now.astimezone(dt.UTC),
        scheduler_id=scheduler_id,
    )


__all__: list[str] = [
    "ModelRuntimeTickScheduleConfig",
    "ModelScheduledFire",
    "scheduled_fire",
]
