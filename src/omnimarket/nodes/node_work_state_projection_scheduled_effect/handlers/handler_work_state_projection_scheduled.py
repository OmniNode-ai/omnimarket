# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-state projection on the runtime's schedule (OMN-22935).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run time;
the tick that is due runs the projector the overlay binds and returns one compact event for the runtime to publish. A
projector that raises (an overlay it needs is absent, a path is refused) leaves the last-run time where it was, so the
next tick tries again. Nothing here names a cadence, a path or a host.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.handlers.scheduled_run_guard import ScheduledRunGuard
from omnimarket.models.work_state_projection import (
    EnumSourceReading,
    EnumWorkStateProjectorProfile,
    ModelWorkStateProjectedEvent,
    ModelWorkStateResult,
    ModelWorkStateScheduleOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
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

logger = logging.getLogger(__name__)


def projector_for(
    profile: EnumWorkStateProjectorProfile,
    environ: Mapping[str, str] | None,
    state_home: str | None = None,
    ledger_read: ModelCommandLedgerReadRoute | None = None,
) -> ProtocolWorkStateProjector:
    """The projector a schedule overlay's profile binds: the in-memory default or the file sources."""
    if profile is EnumWorkStateProjectorProfile.FILE_SOURCES:
        return FileSourcesWorkStateProjector(environ, state_home, ledger_read)
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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._schedule = schedule
        self._projector = projector
        self._environ = environ
        self._clock = clock
        self._last_run: datetime | None = None
        self._refusals = ScheduledOverlayRefusalLatch(SCHEDULE_OVERLAY_ID)
        self._guard = ScheduledRunGuard()

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
            schedule.projector,
            self._environ,
            schedule.state_home,
            schedule.ledger_read,
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

    async def handle_async(
        self, request: ModelRuntimeTick
    ) -> ModelWorkStateProjectedEvent | None:
        """The runtime calls this on its event loop, and handle blocks on the projector's reads.

        So the run executes on a worker thread. A tick that arrives while a run is in flight logs and
        starts no second run; once the run outlives its overlay interval the tick is refused with an
        error that names the age.
        """
        age = self._guard.try_acquire(self._clock())
        if age is not None:
            return self._overlapping(age)
        return await asyncio.to_thread(self._run_guarded, request)

    def _run_guarded(
        self, request: ModelRuntimeTick
    ) -> ModelWorkStateProjectedEvent | None:
        try:
            return self.handle(request)
        finally:
            self._guard.release()

    def _overlapping(self, age: float) -> ModelWorkStateProjectedEvent | None:
        try:
            interval: int | None = self._schedule_overlay().run_interval_seconds
        except NodeOverlayError:
            interval = None
        if interval is None or age < interval:
            logger.warning(
                "%s: tick skipped, the previous run has been in flight for %.1f s",
                SCHEDULE_OVERLAY_ID,
                age,
            )
            return None
        logger.error(
            "%s: the previous run has been in flight for %.1f s, "
            "longer than its %s s interval",
            SCHEDULE_OVERLAY_ID,
            age,
            interval,
        )
        raise RuntimeError(
            f"{SCHEDULE_OVERLAY_ID}: the previous run has been in flight for {age:.0f} s, "
            f"longer than its {interval} s interval"
        )
