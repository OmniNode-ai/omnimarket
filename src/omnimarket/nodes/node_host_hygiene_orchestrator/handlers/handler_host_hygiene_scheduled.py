# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostHygieneScheduled: the runtime-tick route of the host hygiene orchestrator (OMN-23707).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the
last-run time. The tick that is due reads each declared host's newest disk reading from the store, asks
the decision compute (in process) which cleanups are due, sends one lab work unit per due cleanup to its
host, and returns two envelopes: the run event, with every decision and every unit's typed result, and
the schedule run event the schedule verdict reads. A missing overlay is refused once and returns nothing.

A cleanup that ran (completed, failed or timed out) is remembered with the tick time and waits its
policy's interval; a unit that was refused or never reached its host is not remembered, so the next due
tick sends it again.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Mapping

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneRunOutcome,
    EnumHostHygieneUnitOutcome,
    ModelHostHygieneOrchestratorOverlay,
    ModelHostHygieneRunCompleted,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.contract_config import (
    run_completed_topic,
    schedule_run_topic,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_capacity_readings import (
    HostCapacityStore,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_disk_readings import (
    HostDiskReadingStore,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_hygiene_run import (
    DISPATCH_MEMORY,
    DispatchMemory,
    HostHygieneRun,
    NODE_NAME,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.protocols import (
    ProtocolHostHygieneLabWorkSender,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.sender_factory import (
    build_sender,
)

HANDLER_ID = f"{NODE_NAME}.host_hygiene.scheduled_run"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class HandlerHostHygieneScheduled:
    """ORCHESTRATOR handler: a runtime tick in, the hygiene run event and the schedule run event out."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        store: HostDiskReadingStore | None = None,
        capacity: HostCapacityStore | None = None,
        sender: ProtocolHostHygieneLabWorkSender | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
        memory: DispatchMemory | None = None,
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelHostHygieneOrchestratorOverlay, environ=environ
        )
        self._store = store
        self._capacity = capacity
        self._sender = sender
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)
        self._memory = memory if memory is not None else DISPATCH_MEMORY
        self._run = HostHygieneRun(
            gate=self._gate,
            store=self._store,
            capacity=self._capacity,
            sender=self._sender,
            environ=environ,
            clock=self._clock,
            memory=self._memory,
        )

    def _output(
        self, correlation_id: uuid.UUID, events: tuple[ModelEventEnvelope[object], ...]
    ) -> ModelHandlerOutput[None]:
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id=HANDLER_ID,
            events=events,
        )

    async def handle(self, request: ModelRuntimeTick) -> ModelHandlerOutput[None]:
        correlation_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"omnimarket:host-hygiene-tick:{request.tick_id}"
        )
        try:
            overlay = self._run.declaration()
        except NodeOverlayNotFoundError:
            return self._output(correlation_id, ())

        now = request.now.astimezone(dt.UTC)
        if not self._gate.due(now):
            return self._output(correlation_id, ())
        self._gate.mark(now)

        run, units = await self._run.run(
            overlay, now, f"{overlay.job_id}-{request.tick_id}", str(request.tick_id)
        )

        ok = run.outcome in (
            EnumHostHygieneRunOutcome.COMPLETED,
            EnumHostHygieneRunOutcome.NOTHING_DUE,
        )
        completed = sum(
            1 for u in units if u.outcome is EnumHostHygieneUnitOutcome.COMPLETED
        )
        schedule = self._gate.run_event(
            tick_id=str(request.tick_id),
            scheduled_for=request.now,
            fired_at=run.finished_at,
            outcome="completed" if ok else "failed",
            detail=f"units={len(units)} completed={completed} not_completed={len(units) - completed}",
        )
        run_envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
            payload=run,
            correlation_id=correlation_id,
            event_type=run_completed_topic(),
        )
        schedule_envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
            payload=schedule,
            correlation_id=correlation_id,
            event_type=schedule_run_topic(),
        )
        return self._output(correlation_id, (run_envelope, schedule_envelope))


__all__: list[str] = ["HANDLER_ID", "HandlerHostHygieneScheduled"]
