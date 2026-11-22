# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostHygieneScheduled: the runtime-tick route of the host hygiene orchestrator (OMN-23707).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the
last-run time. The tick that is due runs the shared run body (host_hygiene_run) and returns two envelopes:
the run event, with every decision and every unit's typed result, and the schedule run event the schedule
verdict reads. A missing overlay is refused once and returns nothing.

A cleanup that ran (completed, failed or timed out) is remembered with the tick time and waits its
policy's interval; a unit that was refused or never reached its host is not remembered, so the next due
tick sends it again.
"""

from __future__ import annotations

import datetime as dt
import uuid

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneRunOutcome,
    EnumHostHygieneRunTrigger,
    EnumHostHygieneUnitOutcome,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.contract_config import (
    run_completed_topic,
    schedule_run_topic,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_hygiene_run import (
    NODE_NAME,
    HostHygieneRunBase,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.protocols import (
    ProtocolHostHygieneLabWorkSender,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.sender_factory import (
    build_sender,
)


class HandlerHostHygieneScheduled(HostHygieneRunBase):
    """ORCHESTRATOR handler: a runtime tick in, the hygiene run event and the schedule run event out."""

    HANDLER_ID = f"{NODE_NAME}.host_hygiene.scheduled_run"

    async def handle(self, request: ModelRuntimeTick) -> ModelHandlerOutput[None]:
        correlation_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"omnimarket:host-hygiene-tick:{request.tick_id}"
        )
        try:
            overlay = self._overlay()
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return self._output(correlation_id, ())
        self._refusals.recovered()
        now = request.now.astimezone(dt.UTC)
        if not self._gate.due(now):
            return self._output(correlation_id, ())
        self._gate.mark(now)

        sender: ProtocolHostHygieneLabWorkSender = (
            self._sender if self._sender is not None else build_sender(overlay)
        )
        run = await self._run(
            overlay=overlay,
            now=now,
            sender=sender,
            run_id=f"{overlay.job_id}-{request.tick_id}",
            tick_id=str(request.tick_id),
            trigger=EnumHostHygieneRunTrigger.SCHEDULED,
        )
        ok = run.outcome in (
            EnumHostHygieneRunOutcome.COMPLETED,
            EnumHostHygieneRunOutcome.NOTHING_DUE,
        )
        completed = sum(
            1 for u in run.units if u.outcome is EnumHostHygieneUnitOutcome.COMPLETED
        )
        schedule = self._gate.run_event(
            tick_id=str(request.tick_id),
            scheduled_for=request.now,
            fired_at=run.finished_at,
            outcome="completed" if ok else "failed",
            detail=f"units={len(run.units)} completed={completed} not_completed={len(run.units) - completed}",
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
