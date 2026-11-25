# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The receipt routes of the host hygiene orchestrator: a lab work terminal in, the run event out once its last unit has answered.

The tick route publishes each due unit and returns. A pool host answers each unit on the lab work completed
terminal (the unit ran or timed out, with its receipt) or the failed terminal (its handler crashed or its
command was unreadable). These handlers record the answer in the shared pending store; the answer that
completes a run returns the run event and the schedule run event. Terminals of units this node did not send
are ignored: the topics carry every lab work sender's units.
"""

from __future__ import annotations

import datetime as dt
import uuid

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.lab_work.bus import ModelLabWorkCommandFailure
from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneRunOutcome,
    EnumHostHygieneRunTrigger,
    EnumHostHygieneUnitOutcome,
    ModelHostHygieneRunCompleted,
    ModelHostHygieneUnitResult,
    run_outcome,
)
from omnimarket.models.model_lab_work_unit import ModelLabWorkUnitReceipt
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.contract_config import (
    run_completed_topic,
    schedule_run_topic,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.dispatch_host_hygiene import (
    map_receipt,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_hygiene_run import (
    NODE_NAME,
    HostHygieneRunBase,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.pending_runs import (
    PendingRun,
)


def settled_events(
    gate: ScheduleGate, run: PendingRun, *, finished_at: dt.datetime
) -> tuple[ModelEventEnvelope[object], ...]:
    """The run event and the schedule run event of a run whose units have all answered or expired."""
    units = run.results()
    outcome = run_outcome(units)
    completed = sum(
        1 for u in units if u.outcome is EnumHostHygieneUnitOutcome.COMPLETED
    )
    ok = outcome in (
        EnumHostHygieneRunOutcome.COMPLETED,
        EnumHostHygieneRunOutcome.NOTHING_DUE,
    )
    payload = ModelHostHygieneRunCompleted(
        run_id=run.run_id,
        job_id=run.job_id,
        tick_id=run.tick_id,
        started_at=run.started_at,
        finished_at=finished_at,
        outcome=outcome,
        decided=run.decided,
        units=units,
        trigger=EnumHostHygieneRunTrigger.SCHEDULED,
    )
    schedule = gate.run_event(
        tick_id=run.tick_id,
        scheduled_for=run.scheduled_for,
        fired_at=finished_at,
        outcome="completed" if ok else "failed",
        detail=f"units={len(units)} completed={completed} not_completed={len(units) - completed}",
    )
    correlation_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"omnimarket:host-hygiene-tick:{run.tick_id}"
    )
    return (
        ModelEventEnvelope(
            payload=payload,
            correlation_id=correlation_id,
            event_type=run_completed_topic(),
        ),
        ModelEventEnvelope(
            payload=schedule,
            correlation_id=correlation_id,
            event_type=schedule_run_topic(),
        ),
    )


class _ReceiptHandlerBase(HostHygieneRunBase):
    def _answer(self, result: ModelHostHygieneUnitResult) -> ModelHandlerOutput[None]:
        correlation_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"omnimarket:host-hygiene-unit:{result.work_unit_id}"
        )
        recorded = self._pending.record(result)
        if recorded is None or not recorded[1]:
            return self._output(correlation_id, ())
        return self._output(
            correlation_id,
            settled_events(self._gate, recorded[0], finished_at=self._clock()),
        )

    def _unit(self, work_unit_id: str) -> tuple[str, str] | None:
        return self._pending.open_unit(work_unit_id)


class HandlerHostHygieneUnitCompleted(_ReceiptHandlerBase):
    """ORCHESTRATOR handler: a lab work completed terminal in, the run event out when it is the run's last."""

    HANDLER_ID = f"{NODE_NAME}.host_hygiene.unit_completed"

    async def handle(
        self, request: ModelLabWorkUnitReceipt
    ) -> ModelHandlerOutput[None]:
        unit = self._unit(request.work_unit_id)
        if unit is None:
            return self._output(uuid.uuid4(), ())
        host, cleanup_id = unit
        return self._answer(
            map_receipt(host, cleanup_id, request.work_unit_id, request)
        )


class HandlerHostHygieneUnitFailed(_ReceiptHandlerBase):
    """ORCHESTRATOR handler: a lab work failed terminal in; the unit never ran, so it is sent again next due tick."""

    HANDLER_ID = f"{NODE_NAME}.host_hygiene.unit_failed"

    async def handle(
        self, request: ModelLabWorkCommandFailure
    ) -> ModelHandlerOutput[None]:
        unit = self._unit(request.work_unit_id)
        if unit is None:
            return self._output(uuid.uuid4(), ())
        host, cleanup_id = unit
        return self._answer(
            ModelHostHygieneUnitResult(
                host=host,
                cleanup_id=cleanup_id,
                work_unit_id=request.work_unit_id,
                outcome=EnumHostHygieneUnitOutcome.TRANSPORT_UNAVAILABLE,
                exit_code=None,
                detail=f"pool host failure terminal: {request.error_message}"[:2000],
                output_tail="",
            )
        )


__all__: list[str] = [
    "HandlerHostHygieneUnitCompleted",
    "HandlerHostHygieneUnitFailed",
    "settled_events",
]
