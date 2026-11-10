# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The simple floor on the runtime's schedule (OMN-23072, slice 1).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the
last-run time; the tick that is due reads the inputs through the reading source, computes the document with the floor
compute and returns it for the runtime to publish. Nothing here names a cadence, a path or a host.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.floor_17a import ModelFloorSimpleInput, ModelFloorSimpleOverlay
from omnimarket.models.floor_17a.model_floor_document_event import (
    ModelFloorScheduleOverlay,
    ModelFloorSimpleDocumentEvent,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)

SCHEDULE_OVERLAY_ID = "node_floor_simple_scheduled_effect"
SCHEDULE_OVERLAY_FILE_ENV = "ONEX_FLOOR_SCHEDULE_OVERLAY"
FLOOR_OVERLAY_FILE_ENV = "ONEX_FLOOR_SIMPLE_OVERLAY"


@runtime_checkable
class ProtocolFloorReadingSource(Protocol):
    """One read of the floor's inputs for one cycle."""

    def read(
        self, now: datetime, overlay: ModelFloorSimpleOverlay
    ) -> ModelFloorSimpleInput: ...


class UnreadFloorReadings:
    """The in-memory default profile: nothing was read, so the compute draws every source as unread."""

    def read(
        self, now: datetime, overlay: ModelFloorSimpleOverlay
    ) -> ModelFloorSimpleInput:
        return ModelFloorSimpleInput(
            now=now,
            overlay=overlay,
            open_prs=None,
            open_prs_before=None,
            merges_in_window=None,
            watcher_read_at=None,
            slots_working=None,
            slots_total=None,
            record_read_at=None,
            lab=None,
            receipts=None,
            go_given_through=None,
        )


class HandlerFloorSimpleScheduled:
    """Gate on the schedule, read, compute, return the document event."""

    def __init__(
        self,
        *,
        schedule: ModelFloorScheduleOverlay | None = None,
        floor_overlay: ModelFloorSimpleOverlay | None = None,
        source: ProtocolFloorReadingSource | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._schedule = schedule
        self._floor_overlay = floor_overlay
        self._source: ProtocolFloorReadingSource = source or UnreadFloorReadings()
        self._environ = environ
        self._last_run: datetime | None = None

    def _env(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def _schedule_overlay(self) -> ModelFloorScheduleOverlay:
        if self._schedule is None:
            self._schedule = load_structured_overlay(
                SCHEDULE_OVERLAY_ID,
                ModelFloorScheduleOverlay,
                self._env(),
                file_env=SCHEDULE_OVERLAY_FILE_ENV,
            )
        return self._schedule

    def _floor(self, schedule: ModelFloorScheduleOverlay) -> ModelFloorSimpleOverlay:
        if self._floor_overlay is None:
            self._floor_overlay = load_structured_overlay(
                schedule.floor_overlay_id,
                ModelFloorSimpleOverlay,
                self._env(),
                file_env=FLOOR_OVERLAY_FILE_ENV,
            )
        return self._floor_overlay

    def handle(self, request: ModelRuntimeTick) -> ModelFloorSimpleDocumentEvent | None:
        schedule = self._schedule_overlay()
        now = request.now.astimezone(UTC)
        last = self._last_run
        if last is not None and (now - last) < timedelta(
            seconds=schedule.run_interval_seconds
        ):
            return None
        floor_overlay = self._floor(schedule)
        document = HandlerFloorSimple().handle(self._source.read(now, floor_overlay))
        self._last_run = now
        return ModelFloorSimpleDocumentEvent(
            computed_at=now, document=document, tick_id=str(request.tick_id)
        )
