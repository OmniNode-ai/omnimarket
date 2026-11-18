# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Schedule one integration read per observed dev boot and track its idle episode."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from temporalio.service import RPCError

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_merged_change_runner_read_workflow.models.model_runner_read_ready_edge import (
    ModelRunnerReadReadyEdge,
    ModelRunnerReadReadyRequest,
    ModelRunnerReadReadyState,
)

logger = logging.getLogger(__name__)


class HandlerRunnerReadReadyEdge:
    """Record the ready edge and supervise its scheduled read."""

    def __init__(
        self,
        *,
        start: Callable[[ModelRunnerReadReadyEdge], Awaitable[None]],
        completed_at: Callable[[ModelRunnerReadReadyEdge], Awaitable[datetime | None]],
        deliver: Callable[[ModelLabAlarmDeliveryRequest], Awaitable[bool]],
        idle_bound: timedelta,
    ) -> None:
        self._start = start
        self._completed_at = completed_at
        self._deliver = deliver
        self._idle_bound = idle_bound

    async def handle(
        self, request: ModelRunnerReadReadyRequest
    ) -> ModelRunnerReadReadyState:
        state = request.state or ModelRunnerReadReadyState()
        latest, lower = state.latest_boot, state.previous_build_at
        edges = list(state.pending)
        sample = request.snapshot
        if sample is not None and (
            latest is None or sample.started_at >= latest.started_at
        ):
            if latest is None or sample.started_at > latest.started_at:
                lower = latest.built_at if latest is not None else None
                latest = sample.model_copy(update={"ready": False})
            if sample.ready and not latest.ready:
                latest = sample
                if lower is not None:
                    edges.append(
                        ModelRunnerReadReadyEdge(
                            started_at=sample.started_at,
                            previous_build_at=lower,
                            ready_at=request.now,
                        )
                    )
                else:
                    logger.info(
                        "Ready-edge schedule recorded its initial dev build baseline"
                    )

        pending = []
        for edge in edges:
            next_edge = await self._reconcile(edge, request.now)
            if next_edge is not None:
                pending.append(next_edge)
        return ModelRunnerReadReadyState(
            latest_boot=latest, previous_build_at=lower, pending=tuple(pending)
        )

    async def _reconcile(
        self, edge: ModelRunnerReadReadyEdge, now: datetime
    ) -> ModelRunnerReadReadyEdge | None:
        completed = None
        try:
            if not edge.read_started:
                await self._start(edge)
                edge = edge.model_copy(update={"read_started": True})
            completed = await self._completed_at(edge)
        except RPCError as exc:
            logger.warning("Ready-edge read status unavailable: %s", exc.status.name)

        due_at = edge.ready_at + self._idle_bound
        if not edge.alarm_raised and (completed or now) >= due_at:
            if not await self._deliver(self._notice(edge, due_at)):
                return edge
            edge = edge.model_copy(update={"alarm_raised": True})
        if completed is None:
            return edge
        if edge.alarm_raised and not await self._deliver(
            self._notice(edge, due_at, completed_at=completed)
        ):
            return edge
        return None

    @staticmethod
    def _notice(
        edge: ModelRunnerReadReadyEdge,
        due_at: datetime,
        *,
        completed_at: datetime | None = None,
    ) -> ModelLabAlarmDeliveryRequest:
        subject = f"dev runtime boot {edge.started_at.isoformat()}"
        # A later boot can go ready while an earlier read is pending. Clearing one condition
        # must leave the other boot's alarm alone.
        condition = f"merged_change_ready_edge_idle/{edge.started_at.isoformat()}"
        if completed_at is not None:
            return ModelLabAlarmDeliveryRequest(
                correlation_id=condition,
                condition=condition,
                outcome=EnumDeliveryCondition.OK,
                recovery_detail=f"{subject}: integration read completed at {completed_at.isoformat()}",
            )
        detail = f"{subject}: integration read did not complete by {due_at.isoformat()}"
        return ModelLabAlarmDeliveryRequest(
            correlation_id=condition,
            condition=condition,
            outcome=EnumDeliveryCondition.ALARM,
            alarms=(
                ModelLabAlarm(condition=condition, subject=subject, detail=detail),
            ),
            evidence=f"Ready at {edge.ready_at.isoformat()}",
        )
