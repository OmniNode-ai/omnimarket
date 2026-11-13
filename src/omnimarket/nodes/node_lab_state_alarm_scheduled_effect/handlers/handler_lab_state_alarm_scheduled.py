# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab state alarm's tick on the runtime's schedule (OMN-19998).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run
time; the tick that is due loads the alarm overlay, reads the lab verdicts and runs the alarm, returning the run
event. A refused overlay, a missing alarm overlay or an unbound verdict source is a failed run event that names it,
never a silent skip.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.floor_17a.model_floor_17a import (
    ModelAlarmOverlay,
    ModelAlarmState,
    ModelLabSourceVerdict,
    ModelLabStateAlarmRequest,
)
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.handler_lab_state_alarm import (
    HandlerLabStateAlarm,
)
from omnimarket.nodes.node_lab_state_alarm_scheduled_effect.models import (
    ModelLabStateAlarmScheduleOverlay,
)
from omnimarket.nodes.node_lab_state_alarm_scheduled_effect.protocols import (
    ProtocolLabVerdictSource,
)

NODE_NAME = "node_lab_state_alarm_scheduled_effect"


class UnboundLabVerdictSource:
    """The default verdict source: the lab sources are not bound in the runtime."""

    def read(self, now: datetime) -> tuple[ModelLabSourceVerdict, ...] | None:
        return None


class HandlerLabStateAlarmScheduled:
    """Gate on the schedule, load the alarm overlay, read the verdicts, run the alarm, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        source: ProtocolLabVerdictSource | None = None,
        alarm: HandlerLabStateAlarm | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelLabStateAlarmScheduleOverlay, environ=environ
        )
        self._source = source if source is not None else UnboundLabVerdictSource()
        self._alarm = alarm
        self._environ = environ
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)
        self._state = ModelAlarmState()

    def _event(
        self,
        request: ModelRuntimeTick,
        outcome: Literal["completed", "failed"],
        detail: str,
    ) -> ModelScheduleRunEvent:
        return self._gate.run_event(
            tick_id=str(request.tick_id),
            scheduled_for=request.now,
            fired_at=self._clock(),
            outcome=outcome,
            detail=detail,
        )

    def handle(self, request: ModelRuntimeTick) -> ModelScheduleRunEvent | None:
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelLabStateAlarmScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a lab state alarm schedule overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        now = request.now.astimezone(UTC)
        if not self._gate.due(now):
            return None
        self._gate.mark(now)
        try:
            alarm_overlay = load_structured_overlay(
                overlay.alarm_overlay_id, ModelAlarmOverlay, self._environ
            )
        except NodeOverlayNotFoundError:
            return self._event(
                request,
                "failed",
                f"the alarm overlay {overlay.alarm_overlay_id} could not be read",
            )
        verdicts = self._source.read(now)
        if verdicts is None:
            return self._event(
                request, "failed", "no lab verdict source is bound in the runtime"
            )
        alarm = self._alarm
        if alarm is None:
            alarm = HandlerLabStateAlarm()
        result = alarm.handle(
            ModelLabStateAlarmRequest(
                now=now, verdicts=verdicts, state=self._state, overlay=alarm_overlay
            )
        )
        self._state = result.state
        detail = (
            f"raised={len(result.raised)} escalated={len(result.escalated)} "
            f"cleared={len(result.cleared)} undelivered={len(result.undelivered)}"
        )
        return self._event(request, "completed", detail)


__all__: list[str] = ["HandlerLabStateAlarmScheduled", "UnboundLabVerdictSource"]
