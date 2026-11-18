# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostDiskScheduled: the runtime-tick route of the host disk orchestrator (OMN-23866).

Every tick first closes the open runs whose result deadline has passed (each unanswered command fails as
``no_result_by_deadline``). A tick inside the overlay's interval then starts nothing. The tick that is due starts a run.
A missing orchestrator overlay or decision policy is refused once and returns nothing.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Mapping

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.model_host_disk_decision import ModelHostDiskPolicy
from omnimarket.models.model_host_disk_orchestrator import (
    EnumHostDiskRunOutcome,
    ModelHostDiskOrchestratorOverlay,
    ModelHostDiskRunCompleted,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.contract_config import (
    decision_overlay_id,
    schedule_run_topic,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.handler_host_disk_record_read import (
    HOST_DISK_STATE,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.run_core import (
    close_run,
    start_run,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.run_state import (
    HostDiskOrchestratorState,
)

NODE_NAME = "node_host_disk_orchestrator"
HANDLER_ID = f"{NODE_NAME}.host_disk.scheduled_run"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def load_policy(environ: Mapping[str, str] | None = None) -> ModelHostDiskPolicy:
    """The decision policy: overlays/node_host_disk_decision_compute/overlay.yaml, never a value in code."""
    return load_structured_overlay(decision_overlay_id(), ModelHostDiskPolicy, environ)


class HandlerHostDiskScheduled:
    """ORCHESTRATOR handler: a runtime tick in; commands, alarms and run records out."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        state: HostDiskOrchestratorState | None = None,
        policy_loader: Callable[[], ModelHostDiskPolicy] | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelHostDiskOrchestratorOverlay, environ=environ
        )
        self._state = HOST_DISK_STATE if state is None else state
        self._policy_loader = policy_loader or (lambda: load_policy(environ))
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)

    def handle(self, request: ModelRuntimeTick) -> ModelHandlerOutput[None]:
        correlation_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"omnimarket:host-disk-tick:{request.tick_id}"
        )
        now = request.now.astimezone(dt.UTC)
        events: list[ModelEventEnvelope[object]] = [
            close_run(o, finished_at=now) for o in self._state.runs.overdue(now)
        ]
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelHostDiskOrchestratorOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a host disk orchestrator overlay"
                )
            policy = self._policy_loader()
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return self._output(correlation_id, tuple(events))
        self._refusals.recovered()
        if not self._gate.due(now):
            return self._output(correlation_id, tuple(events))
        self._gate.mark(now)
        started = start_run(
            self._state,
            overlay,
            policy,
            now=now,
            trigger="scheduled",
            run_key=str(request.tick_id),
        )
        events.extend(started)
        refused = any(
            isinstance(e.payload, ModelHostDiskRunCompleted)
            and e.payload.outcome is EnumHostDiskRunOutcome.POLICY_REFUSED
            for e in started
        )
        events.append(
            ModelEventEnvelope(
                payload=self._gate.run_event(
                    tick_id=str(request.tick_id),
                    scheduled_for=request.now,
                    fired_at=self._clock(),
                    outcome="failed" if refused else "completed",
                    detail="policy refused" if refused else f"events={len(started)}",
                ),
                correlation_id=correlation_id,
                event_type=schedule_run_topic(),
            )
        )
        return self._output(correlation_id, tuple(events))

    def _output(
        self,
        correlation_id: uuid.UUID,
        events: tuple[ModelEventEnvelope[object], ...],
    ) -> ModelHandlerOutput[None]:
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id=HANDLER_ID,
            events=events,
        )


__all__: list[str] = ["NODE_NAME", "HandlerHostDiskScheduled", "load_policy"]
