# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostDriftScheduled: the runtime-tick route of the host drift orchestrator (OMN-24197).

handle takes the runtime tick. A tick inside the overlay's interval, or one this handler already
claimed, returns nothing, so two deliveries of one tick send one set of units. The tick that is due
is claimed under a lock, sends one lab work unit per declared host, and returns two envelopes: the
run event, with one typed verdict per host, and the schedule run event the schedule verdict reads.
A run with a host that sent no result inside the overlay timeout ends failed and names that host in
the schedule event, with the other hosts' verdicts on the run event.

A failure between the claim and the send releases the claim, ends the run failed, and leaves the
next due tick to send again. A missing overlay is refused once and returns nothing.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections.abc import Callable, Mapping

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.model_host_drift_orchestrator import (
    EnumHostDriftHostVerdict,
    EnumHostDriftRunOutcome,
    ModelHostDriftHostVerdict,
    ModelHostDriftOrchestratorOverlay,
    ModelHostDriftRunCompleted,
    failed_hosts,
    run_outcome,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.contract_config import (
    run_completed_topic,
    schedule_run_topic,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.dispatch_host_drift import (
    DispatchHostDriftUnits,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.protocols import (
    ProtocolHostDriftLabWorkSender,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.sender_factory import (
    build_sender,
)

NODE_NAME = "node_host_drift_orchestrator"
HANDLER_ID = f"{NODE_NAME}.host_drift.scheduled_run"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class HandlerHostDriftScheduled:
    """ORCHESTRATOR handler: a runtime tick in, the drift run event and the schedule run event out."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        sender: ProtocolHostDriftLabWorkSender | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelHostDriftOrchestratorOverlay, environ=environ
        )
        self._sender = sender
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)
        self._claim = threading.Lock()
        self._claimed_tick: str | None = None

    def _output(
        self, correlation_id: uuid.UUID, events: tuple[ModelEventEnvelope[object], ...]
    ) -> ModelHandlerOutput[None]:
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id=HANDLER_ID,
            events=events,
        )

    def _claim_tick(self, tick_id: str, now: dt.datetime) -> bool:
        """Claim the tick for this handler: false for a tick already claimed or one inside the interval."""
        with self._claim:
            if tick_id == self._claimed_tick or not self._gate.due(now):
                return False
            self._gate.mark(now)
            self._claimed_tick = tick_id
            return True

    def _release_claim(self) -> None:
        with self._claim:
            self._gate.rearm()

    def handle(self, request: ModelRuntimeTick) -> ModelHandlerOutput[None]:
        correlation_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"omnimarket:host-drift-tick:{request.tick_id}"
        )
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelHostDriftOrchestratorOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a host drift orchestrator overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return self._output(correlation_id, ())
        self._refusals.recovered()
        now = request.now.astimezone(dt.UTC)
        if not self._claim_tick(str(request.tick_id), now):
            return self._output(correlation_id, ())

        stopped = ""
        try:
            sender = self._sender if self._sender is not None else build_sender(overlay)
            verdicts = DispatchHostDriftUnits(overlay, sender).dispatch(overlay.hosts)
        except Exception as exc:
            self._release_claim()
            stopped = f"{type(exc).__name__}: {exc}"[:1000]
            verdicts = tuple(
                ModelHostDriftHostVerdict(
                    host=row.name,
                    verdict=EnumHostDriftHostVerdict.NO_RESULT,
                    work_unit_id="",
                    detail="the run stopped before this host was sent",
                )
                for row in overlay.hosts
            )

        outcome = run_outcome(verdicts)
        silent = failed_hosts(verdicts)
        finished_at = self._clock()
        run = ModelHostDriftRunCompleted(
            run_id=f"{overlay.job_id}-{request.tick_id}",
            job_id=overlay.job_id,
            tick_id=str(request.tick_id),
            started_at=now,
            finished_at=finished_at,
            outcome=outcome,
            verdicts=verdicts,
            failed_hosts=silent,
        )
        answered = len(verdicts) - len(silent)
        detail = f"hosts={len(verdicts)} answered={answered}"
        if silent:
            detail += f" no_answer={','.join(silent)}"
        if stopped:
            detail += f" stopped={stopped}"
        schedule = self._gate.run_event(
            tick_id=str(request.tick_id),
            scheduled_for=request.now,
            fired_at=finished_at,
            outcome="completed"
            if outcome is EnumHostDriftRunOutcome.COMPLETED
            else "failed",
            detail=detail,
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


__all__: list[str] = ["NODE_NAME", "HandlerHostDriftScheduled"]
