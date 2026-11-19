# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the broker-lag check and the configured restart probe on runtime ticks.

Each job tracks its own last run. A due job reads the alarm overlay through the
shared tick module and returns a schedule-run event, including a failed event
when the alarm overlay is unreadable.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.lab_alarm_scheduled_tick import (
    ModelLabAlarmTickOutcome,
    run_container_restarts_probe_tick,
    run_lab_alarm_tick,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.handlers.scheduled_run_guard import ScheduledRunGuard
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    HandlerBrokerLag,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import (
    build_handler as build_lab_alarm_handler,
)
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm_conditions import (
    build_container_restarts_handler,
)
from omnimarket.nodes.node_lab_alarm_scheduled_effect.models import (
    ModelLabAlarmScheduleOverlay,
)

NODE_NAME = "node_lab_alarm_scheduled_effect"

logger = logging.getLogger(__name__)


class HandlerLabAlarmScheduled:
    """Run the lab alarm jobs due on this tick."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        tick: Callable[..., ModelLabAlarmTickOutcome] = run_lab_alarm_tick,
        restarts_tick: Callable[
            ..., ModelLabAlarmTickOutcome
        ] = run_container_restarts_probe_tick,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        build_handler: Callable[
            [ModelLabAlarmOverlay], HandlerBrokerLag
        ] = build_lab_alarm_handler,
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelLabAlarmScheduleOverlay, environ=environ
        )
        self._tick = tick
        self._restarts_tick = restarts_tick
        self._restarts_gate: ScheduleGate | None = None
        self._environ = environ
        self._clock = clock
        self._build_handler = build_handler
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)
        self._guard = ScheduledRunGuard()

    def _event(
        self,
        request: ModelRuntimeTick,
        outcome: Literal["completed", "failed"],
        detail: str,
        *,
        gate: ScheduleGate | None = None,
    ) -> ModelScheduleRunEvent:
        return (gate or self._gate).run_event(
            tick_id=str(request.tick_id),
            scheduled_for=request.now,
            fired_at=self._clock(),
            outcome=outcome,
            detail=detail,
        )

    def handle(
        self, request: ModelRuntimeTick
    ) -> ModelScheduleRunEvent | ModelHandlerOutput[None] | None:
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelLabAlarmScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a lab alarm schedule overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        lag = self._run_due(
            request, overlay, self._gate, self._tick, self._build_handler
        )
        if overlay.container_restarts_probe is None:
            return lag
        if self._restarts_gate is None:
            self._restarts_gate = ScheduleGate(
                NODE_NAME, declaration=overlay.container_restarts_probe
            )
        restarts = self._run_due(
            request,
            overlay,
            self._restarts_gate,
            self._restarts_tick,
            build_container_restarts_handler,
        )
        if lag is None or restarts is None:
            return lag or restarts
        return ModelHandlerOutput.for_effect(
            input_envelope_id=request.tick_id,
            correlation_id=request.correlation_id,
            handler_id=NODE_NAME,
            events=(lag, restarts),
        )

    def _run_due(
        self,
        request: ModelRuntimeTick,
        overlay: ModelLabAlarmScheduleOverlay,
        gate: ScheduleGate,
        tick: Callable[..., ModelLabAlarmTickOutcome],
        build_handler: Callable[..., object],
    ) -> ModelScheduleRunEvent | None:
        now = request.now.astimezone(UTC)
        if not gate.due(now):
            return None
        gate.mark(now)
        try:
            outcome = tick(
                alarm_overlay_id=overlay.alarm_overlay_id,
                environ=self._environ,
                clock=self._clock,
                build_handler=build_handler,
            )
        except NodeOverlayError:
            return self._event(
                request,
                "failed",
                "the lab alarm overlay could not be read",
                gate=gate,
            )
        return self._event(
            request,
            "failed" if outcome.failed else "completed",
            outcome.detail,
            gate=gate,
        )

    async def handle_async(
        self, request: ModelRuntimeTick
    ) -> ModelScheduleRunEvent | ModelHandlerOutput[None] | None:
        """The runtime calls this on its event loop, and handle blocks on the alarm run's network I/O.

        So the run executes on a worker thread. A tick that arrives while a run is in flight logs and
        starts no second run; once the run outlives its overlay interval the tick returns a failed run
        event naming the age.
        """
        age = self._guard.try_acquire(self._clock())
        if age is not None:
            return self._overlapping(request, age)
        return await asyncio.to_thread(self._run_guarded, request)

    def _run_guarded(
        self, request: ModelRuntimeTick
    ) -> ModelScheduleRunEvent | ModelHandlerOutput[None] | None:
        try:
            return self.handle(request)
        finally:
            self._guard.release()

    def _overlapping(
        self, request: ModelRuntimeTick, age: float
    ) -> ModelScheduleRunEvent | None:
        try:
            interval: int | None = self._gate.declaration().run_interval_seconds
        except NodeOverlayError:
            interval = None
        if interval is None or age < interval:
            logger.warning(
                "%s: tick skipped, the previous run has been in flight for %.1f s",
                NODE_NAME,
                age,
            )
            return None
        logger.error(
            "%s: the previous run has been in flight for %.1f s, "
            "longer than its %s s interval",
            NODE_NAME,
            age,
            interval,
        )
        return self._event(
            request,
            "failed",
            f"the previous run has been in flight for {age:.0f} s, "
            f"longer than its {interval} s interval",
        )


__all__: list[str] = ["HandlerLabAlarmScheduled"]
