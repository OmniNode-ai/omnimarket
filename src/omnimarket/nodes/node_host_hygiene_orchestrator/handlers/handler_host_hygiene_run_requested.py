# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostHygieneRunRequested: the on-demand route of the host hygiene orchestrator (OMN-24557).

handle takes a ModelHostHygieneRunRequested payload. It reads the overlay through the gate's declaration
(a missing overlay is refused: return the output with no events and log once, exactly like the scheduled
handler's refusal path), does NOT call gate.due() or gate.mark() (an asked-for run never moves the
schedule), runs HostHygieneRun.run for now = the handler clock, and returns ONE envelope: the run-completed
event (topic from run_completed_topic()) whose correlation_id is the INPUT envelope's correlation id.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Mapping

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.model_host_hygiene_orchestrator import (
    ModelHostHygieneOrchestratorOverlay,
    ModelHostHygieneRunRequested,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.contract_config import (
    run_completed_topic,
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

HANDLER_ID = f"{NODE_NAME}.host_hygiene.run_requested"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class HandlerHostHygieneRunRequested:
    """ON-DEMAND handler: a run-requested command in, the run-completed event out."""

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

    async def handle(
        self, envelope: ModelEventEnvelope[ModelHostHygieneRunRequested]
    ) -> ModelHandlerOutput[None]:
        correlation_id = envelope.correlation_id
        try:
            overlay = self._run.declaration()
        except NodeOverlayNotFoundError:
            return self._output(correlation_id, ())

        now = self._clock().astimezone(dt.UTC)
        run_id = f"{overlay.job_id}-ondemand-{correlation_id}"
        run, _ = await self._run.run(overlay, now, run_id, "ondemand")

        run_envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
            payload=run,
            correlation_id=correlation_id,
            event_type=run_completed_topic(),
        )
        return self._output(correlation_id, (run_envelope,))


__all__: list[str] = ["HANDLER_ID", "HandlerHostHygieneRunRequested"]
