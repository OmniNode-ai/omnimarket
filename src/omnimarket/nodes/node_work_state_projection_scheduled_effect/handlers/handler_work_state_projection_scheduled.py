# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-state projection on the runtime's schedule (OMN-22935).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run time;
the tick that is due runs the projector the overlay binds and returns one compact event for the runtime to publish. A
projector that raises (an overlay it needs is absent, a path is refused) leaves the last-run time where it was, so the
next tick tries again. Nothing here names a cadence, a path or a host.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.work_state_projection import (
    EnumSourceReading,
    EnumWorkStateProjectorProfile,
    ModelWorkStateProjectedEvent,
    ModelWorkStateResult,
    ModelWorkStateScheduleOverlay,
)
from omnimarket.nodes.node_work_state_projection_scheduled_effect.protocols.adapter_file_sources_projector import (
    FileSourcesWorkStateProjector,
)
from omnimarket.nodes.node_work_state_projection_scheduled_effect.protocols.protocol_work_state_projector import (
    ProtocolWorkStateProjector,
    UnreadWorkStateProjector,
)

SCHEDULE_OVERLAY_ID = "node_work_state_projection_scheduled_effect"
SCHEDULE_OVERLAY_FILE_ENV = "ONEX_WORK_STATE_SCHEDULE_OVERLAY"


def projector_for(
    profile: EnumWorkStateProjectorProfile,
    environ: Mapping[str, str] | None,
    state_home: str | None = None,
) -> ProtocolWorkStateProjector:
    """The projector a schedule overlay's profile binds: the in-memory default or the file sources."""
    if profile is EnumWorkStateProjectorProfile.FILE_SOURCES:
        return FileSourcesWorkStateProjector(environ, state_home)
    return UnreadWorkStateProjector()


def summarize(
    result: ModelWorkStateResult,
    *,
    computed_at: datetime,
    tick_id: str,
    projector: EnumWorkStateProjectorProfile,
) -> ModelWorkStateProjectedEvent:
    """The compact event for one projection: counts, ready count, and every source that was not fresh."""
    return ModelWorkStateProjectedEvent(
        computed_at=computed_at,
        tick_id=tick_id,
        projector=projector,
        as_of=result.as_of,
        item_count=len(result.items),
        counts=dict(result.counts),
        ready_in_scope=len(result.ready()),
        unread=result.unread,
        stale_sources=tuple(
            s.source.value
            for s in result.source_status
            if s.reading is EnumSourceReading.STALE
        ),
    )


class HandlerWorkStateProjectionScheduled:
    """Gate on the schedule, project, return the projected event."""

    def __init__(
        self,
        *,
        schedule: ModelWorkStateScheduleOverlay | None = None,
        projector: ProtocolWorkStateProjector | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._schedule = schedule
        self._projector = projector
        self._environ = environ
        self._last_run: datetime | None = None
        self._refusals = ScheduledOverlayRefusalLatch(SCHEDULE_OVERLAY_ID)

    def _env(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def _schedule_overlay(self) -> ModelWorkStateScheduleOverlay:
        if self._schedule is None:
            self._schedule = load_structured_overlay(
                SCHEDULE_OVERLAY_ID,
                ModelWorkStateScheduleOverlay,
                self._env(),
                file_env=SCHEDULE_OVERLAY_FILE_ENV,
            )
        return self._schedule

    def handle(self, request: ModelRuntimeTick) -> ModelWorkStateProjectedEvent | None:
        try:
            schedule = self._schedule_overlay()
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        now = request.now.astimezone(UTC)
        last = self._last_run
        if last is not None and (now - last) < timedelta(
            seconds=schedule.run_interval_seconds
        ):
            return None
        projector = self._projector or projector_for(
            schedule.projector, self._environ, schedule.state_home
        )
        try:
            result = projector.project(now)
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        self._last_run = now
        self._refusals.recovered()
        return summarize(
            result,
            computed_at=now,
            tick_id=str(request.tick_id),
            projector=schedule.projector,
        )
