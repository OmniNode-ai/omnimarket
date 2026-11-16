# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerConvergenceScheduled: the runtime-tick route of the convergence orchestrator (OMN-24198).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the
last-run time. The tick that is due sends one convergence command per declared host (the observe command
or the act command, by the overlay's mode) as a lab work unit and returns two envelopes: the run event,
with one typed result per host, and the schedule run event the schedule verdict reads. The run ends
``failed`` when any host's result is a refusal, a failure, a timeout or an unreachable host, and
``completed`` otherwise. A missing overlay is refused once and returns nothing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Callable, Mapping

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.model_convergence_orchestrator import (
    EnumConvergenceRunOutcome,
    ModelConvergenceOrchestratorOverlay,
    ModelConvergenceRunCompleted,
    run_outcome,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.contract_config import (
    run_completed_topic,
    schedule_run_topic,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.dispatch_convergence import (
    DispatchConvergenceUnits,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.protocols import (
    ProtocolConvergenceLabWorkSender,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.sender_factory import (
    build_sender,
)

NODE_NAME = "node_convergence_orchestrator"
HANDLER_ID = f"{NODE_NAME}.convergence.scheduled_run"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class HandlerConvergenceScheduled:
    """ORCHESTRATOR handler: a runtime tick in, the convergence run event and the schedule run event out."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        sender: ProtocolConvergenceLabWorkSender | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelConvergenceOrchestratorOverlay, environ=environ
        )
        self._sender = sender
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)

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
            uuid.NAMESPACE_URL, f"omnimarket:convergence-tick:{request.tick_id}"
        )
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelConvergenceOrchestratorOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a convergence orchestrator overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return self._output(correlation_id, ())
        self._refusals.recovered()
        now = request.now.astimezone(dt.UTC)
        if not self._gate.due(now):
            return self._output(correlation_id, ())
        self._gate.mark(now)

        sender = self._sender if self._sender is not None else build_sender(overlay)
        # sender.send blocks until the host's receipt; the runtime calls handle on its loop, so run the dispatch off it.
        hosts = await asyncio.to_thread(
            DispatchConvergenceUnits(overlay, sender).dispatch
        )
        outcome = run_outcome(hosts)
        finished_at = self._clock()
        run = ModelConvergenceRunCompleted(
            run_id=f"{overlay.job_id}-{request.tick_id}",
            job_id=overlay.job_id,
            tick_id=str(request.tick_id),
            mode=overlay.mode,
            started_at=now,
            finished_at=finished_at,
            outcome=outcome,
            hosts=hosts,
        )
        counts = ", ".join(
            f"{name}={sum(1 for h in hosts if h.outcome.value == name)}"
            for name in sorted({h.outcome.value for h in hosts})
        )
        schedule = self._gate.run_event(
            tick_id=str(request.tick_id),
            scheduled_for=request.now,
            fired_at=finished_at,
            outcome=(
                "completed"
                if outcome is EnumConvergenceRunOutcome.COMPLETED
                else "failed"
            ),
            detail=f"mode={overlay.mode.value} hosts={len(hosts)} {counts}",
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


__all__: list[str] = ["NODE_NAME", "HandlerConvergenceScheduled"]
