# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm rule of the floor fill (OMN-24873), pure: no I/O, the time comes from the request.

IDLE: the last ``idle_runs`` runs by time all saw more than ``slot_threshold`` free slots and all sent 0 lanes; the row
names the latest run's cause. UNCLAIMED: a started lane with no CLAIM read more than ``claim_deadline_minutes`` after
its start, or whose CLAIM arrived later than that; a CLAIM at the deadline or sooner is on time. NOT_RUNNING (only when
``max_run_age_minutes`` is set): no run observed in that many minutes before the check, which the IDLE rule cannot see.
"""

from __future__ import annotations

from datetime import timedelta

from omnimarket.nodes.node_floor_fill_alarm_compute.models import (
    EnumFloorFillAlarmKind,
    ModelFloorFillAlarmRequest,
    ModelFloorFillAlarmResult,
    ModelFloorFillAlarmRow,
    ModelFloorFillSentLane,
)

NO_CAUSE = "no cause recorded"


class HandlerFloorFillAlarm:
    """Answer the alarm rows of one check over the fill's runs and started lanes."""

    def handle(self, request: ModelFloorFillAlarmRequest) -> ModelFloorFillAlarmResult:
        alarms: list[ModelFloorFillAlarmRow] = []
        stale = self._not_running(request)
        if stale is not None:
            alarms.append(stale)
        idle = self._idle(request)
        if idle is not None:
            alarms.append(idle)
        for lane in request.sent_lanes:
            row = self._unclaimed(request, lane)
            if row is not None:
                alarms.append(row)
        return ModelFloorFillAlarmResult(
            alarms=tuple(alarms),
            runs_read=len(request.runs),
            lanes_read=len(request.sent_lanes),
        )

    @staticmethod
    def _not_running(
        request: ModelFloorFillAlarmRequest,
    ) -> ModelFloorFillAlarmRow | None:
        if request.max_run_age_minutes is None:
            return None
        bound = request.now - timedelta(minutes=request.max_run_age_minutes)
        latest = max((run.observed_at for run in request.runs), default=None)
        if latest is not None and latest >= bound:
            return None
        seen = "no run read" if latest is None else f"last run {latest.isoformat()}"
        cause = f"no floor fill run in the last {request.max_run_age_minutes} minutes ({seen})"
        return ModelFloorFillAlarmRow(
            kind=EnumFloorFillAlarmKind.NOT_RUNNING,
            subject="floor-fill",
            cause=cause,
            row_text=f"floor fill is not running: {cause}",
        )

    @staticmethod
    def _idle(request: ModelFloorFillAlarmRequest) -> ModelFloorFillAlarmRow | None:
        runs = sorted(request.runs, key=lambda run: run.observed_at)[
            -request.idle_runs :
        ]
        if len(runs) < request.idle_runs:
            return None
        if not all(
            run.free_slots > request.slot_threshold and run.lanes_sent == 0
            for run in runs
        ):
            return None
        cause = runs[-1].cause or NO_CAUSE
        free = ",".join(str(run.free_slots) for run in runs)
        return ModelFloorFillAlarmRow(
            kind=EnumFloorFillAlarmKind.IDLE,
            subject="floor-fill",
            cause=cause,
            row_text=(
                f"floor fill sent 0 lanes for {request.idle_runs} runs in a row while free Claude slots stayed "
                f"above {request.slot_threshold} (free {free}): cause {cause}"
            ),
        )

    @staticmethod
    def _unclaimed(
        request: ModelFloorFillAlarmRequest, lane: ModelFloorFillSentLane
    ) -> ModelFloorFillAlarmRow | None:
        deadline = lane.sent_at + timedelta(minutes=request.claim_deadline_minutes)
        if lane.claim_at is None:
            if request.now <= deadline:
                return None
            minutes = int((request.now - lane.sent_at).total_seconds() // 60)
            cause = f"no CLAIM key={lane.key} {minutes} minutes after the start"
        else:
            if lane.claim_at <= deadline:
                return None
            minutes = int((lane.claim_at - lane.sent_at).total_seconds() // 60)
            cause = f"CLAIM key={lane.key} arrived {minutes} minutes after the start"
        return ModelFloorFillAlarmRow(
            kind=EnumFloorFillAlarmKind.UNCLAIMED,
            subject=lane.lane,
            cause=cause,
            row_text=f"lane {lane.lane} {cause}",
        )


__all__: list[str] = ["HandlerFloorFillAlarm"]
