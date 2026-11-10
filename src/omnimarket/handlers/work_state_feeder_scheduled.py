# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A work-state feeder on the runtime's schedule (OMN-23427).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run time;
the tick that is due runs the feeder the profile binds and returns one compact event for the runtime to publish. A
feeder that raises leaves the last-run time where it was, so the next tick tries again -- the runtime reports the
raise. A fed source whose newest fact is older than the overlay's stale bound is reported stale. Nothing here names a
cadence, a path or a host: the feeder, the overlay id and its file variable are the caller's.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.work_state_feeder import (
    EnumFeeder,
    EnumFeederProfile,
    ModelFeederScheduleOverlay,
    ModelWorkStateFeederEvent,
)
from omnimarket.protocols.protocol_work_state_feeder import (
    NotFedWorkStateFeeder,
    ProtocolWorkStateFeeder,
)


class HandlerWorkStateFeederScheduled:
    """Gate on the schedule, feed, return the feeder's event."""

    def __init__(
        self,
        *,
        feeder: EnumFeeder,
        overlay_id: str,
        overlay_file_env: str,
        live: Callable[[Mapping[str, str] | None], ProtocolWorkStateFeeder],
        schedule: ModelFeederScheduleOverlay | None = None,
        feeder_port: ProtocolWorkStateFeeder | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._feeder = feeder
        self._overlay_id = overlay_id
        self._overlay_file_env = overlay_file_env
        self._live = live
        self._schedule = schedule
        self._feeder_port = feeder_port
        self._environ = environ
        self._last_run: datetime | None = None

    def _env(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def _schedule_overlay(self) -> ModelFeederScheduleOverlay:
        if self._schedule is None:
            self._schedule = load_structured_overlay(
                self._overlay_id,
                ModelFeederScheduleOverlay,
                self._env(),
                file_env=self._overlay_file_env,
            )
        return self._schedule

    def _port(self, schedule: ModelFeederScheduleOverlay) -> ProtocolWorkStateFeeder:
        """The feeder the schedule's profile binds: the caller's port, the in-memory default, or the live feeder."""
        if self._feeder_port is not None:
            return self._feeder_port
        if schedule.profile is EnumFeederProfile.IN_MEMORY:
            return NotFedWorkStateFeeder()
        return self._live(self._environ)

    def handle(self, request: ModelRuntimeTick) -> ModelWorkStateFeederEvent | None:
        schedule = self._schedule_overlay()
        now = request.now.astimezone(UTC)
        last = self._last_run
        if last is not None and (now - last) < timedelta(
            seconds=schedule.run_interval_seconds
        ):
            return None
        outcome = self._port(schedule).feed(now)
        self._last_run = now
        status: Literal["fed", "stale", "not_fed", "failed"]
        if outcome.status == "fed":
            # The outcome model refuses a fed outcome without its source_as_of; the check here is for the type only.
            source_as_of = outcome.source_as_of
            aged = source_as_of is None or now - source_as_of > timedelta(
                seconds=schedule.max_source_age_s
            )
            status = "stale" if aged else "fed"
        else:
            status = outcome.status
        return ModelWorkStateFeederEvent(
            feeder=self._feeder,
            computed_at=now,
            tick_id=str(request.tick_id),
            profile=schedule.profile,
            status=status,
            source_as_of=outcome.source_as_of,
            item_count=outcome.item_count,
            cause=outcome.cause,
        )
