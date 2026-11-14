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
from omnimarket.models.model_host_hygiene import (
    ModelHostHygieneCleanupPolicy,
    ModelHostHygieneDecisionRequest,
    ModelHostHygieneHostFacts,
    ModelHostHygieneLastDispatch,
)
from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneRunOutcome,
    EnumHostHygieneUnitOutcome,
    ModelHostHygieneOrchestratorOverlay,
    ModelHostHygieneRunCompleted,
    run_outcome,
)
from omnimarket.nodes.node_host_hygiene_decision_compute.handlers.handler_host_hygiene_decision import (
    decide_host_hygiene,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.contract_config import (
    run_completed_topic,
    schedule_run_topic,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.dispatch_host_hygiene import (
    DispatchHostHygieneUnits,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.handler_host_hygiene_record_read import (
    HOST_DISK_READINGS,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_disk_readings import (
    HostDiskReadingStore,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.protocols import (
    ProtocolHostHygieneLabWorkSender,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.sender_factory import (
    build_sender,
)

NODE_NAME = "node_host_hygiene_orchestrator"
HANDLER_ID = f"{NODE_NAME}.host_hygiene.scheduled_run"
#: The outcomes that mean the command ran on the host, so the cleanup waits its interval.
_RAN = frozenset(
    {
        EnumHostHygieneUnitOutcome.COMPLETED,
        EnumHostHygieneUnitOutcome.FAILED,
        EnumHostHygieneUnitOutcome.TIMED_OUT,
    }
)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class HandlerHostHygieneScheduled:
    """ORCHESTRATOR handler: a runtime tick in, the hygiene run event and the schedule run event out."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        store: HostDiskReadingStore | None = None,
        sender: ProtocolHostHygieneLabWorkSender | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelHostHygieneOrchestratorOverlay, environ=environ
        )
        self._store = HOST_DISK_READINGS if store is None else store
        self._sender = sender
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)
        self._dispatched: dict[tuple[str, str], dt.datetime] = {}

    def _output(
        self, correlation_id: uuid.UUID, events: tuple[ModelEventEnvelope[object], ...]
    ) -> ModelHandlerOutput[None]:
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id=HANDLER_ID,
            events=events,
        )

    def _facts(
        self, overlay: ModelHostHygieneOrchestratorOverlay
    ) -> tuple[ModelHostHygieneHostFacts, ...]:
        return tuple(
            ModelHostHygieneHostFacts(
                host=row.name,
                cleanup_ids=row.cleanup_ids,
                reading=self._store.reading_for(row.name, row.pressure_volume),
                last_dispatched=tuple(
                    ModelHostHygieneLastDispatch(
                        cleanup_id=cleanup_id,
                        dispatched_at=self._dispatched[(row.name, cleanup_id)],
                    )
                    for cleanup_id in row.cleanup_ids
                    if (row.name, cleanup_id) in self._dispatched
                ),
            )
            for row in overlay.hosts
        )

    def handle(self, request: ModelRuntimeTick) -> ModelHandlerOutput[None]:
        correlation_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"omnimarket:host-hygiene-tick:{request.tick_id}"
        )
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelHostHygieneOrchestratorOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a host hygiene orchestrator overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return self._output(correlation_id, ())
        self._refusals.recovered()
        now = request.now.astimezone(dt.UTC)
        if not self._gate.due(now):
            return self._output(correlation_id, ())
        self._gate.mark(now)

        decision = decide_host_hygiene(
            ModelHostHygieneDecisionRequest(
                now=now,
                max_reading_age_seconds=overlay.max_reading_age_seconds,
                cleanups=tuple(
                    ModelHostHygieneCleanupPolicy(
                        cleanup_id=c.cleanup_id,
                        min_used_fraction=c.min_used_fraction,
                        min_interval_seconds=c.min_interval_seconds,
                        when_unread=c.when_unread,
                    )
                    for c in overlay.cleanups
                ),
                hosts=self._facts(overlay),
            )
        )
        sender = self._sender if self._sender is not None else build_sender(overlay)
        units = DispatchHostHygieneUnits(overlay, sender).dispatch(decision.due_items())
        for unit in units:
            if unit.outcome in _RAN:
                self._dispatched[(unit.host, unit.cleanup_id)] = now

        outcome = run_outcome(units)
        finished_at = self._clock()
        run = ModelHostHygieneRunCompleted(
            run_id=f"{overlay.job_id}-{request.tick_id}",
            job_id=overlay.job_id,
            tick_id=str(request.tick_id),
            started_at=now,
            finished_at=finished_at,
            outcome=outcome,
            decided=decision.items,
            units=units,
        )
        ok = outcome in (
            EnumHostHygieneRunOutcome.COMPLETED,
            EnumHostHygieneRunOutcome.NOTHING_DUE,
        )
        completed = sum(
            1 for u in units if u.outcome is EnumHostHygieneUnitOutcome.COMPLETED
        )
        schedule = self._gate.run_event(
            tick_id=str(request.tick_id),
            scheduled_for=request.now,
            fired_at=finished_at,
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


__all__: list[str] = ["NODE_NAME", "HandlerHostHygieneScheduled"]
