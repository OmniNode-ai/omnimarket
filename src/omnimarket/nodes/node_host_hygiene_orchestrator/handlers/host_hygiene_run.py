# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one shared body of a host hygiene orchestrator run (OMN-24610).

Both routes of the node subclass ``HostHygieneRunBase``: the runtime-tick route
(handler_host_hygiene_scheduled) and the on-demand command route
(handler_host_hygiene_run_requested). The base reads the overlay through the
schedule gate, reads each declared host's newest disk reading from the store
and its capacity guard through the hold reader, asks the decision compute (in
process) which cleanups are due, sends one lab work unit per due cleanup to its
host and builds the run event. Each route chooses its trigger, resolves the
sender and decides which envelopes leave with the run.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Callable, Mapping

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

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
    EnumHostHygieneRunTrigger,
    EnumHostHygieneUnitOutcome,
    ModelHostHygieneOrchestratorOverlay,
    ModelHostHygieneRunCompleted,
    run_outcome,
)
from omnimarket.nodes.node_host_hygiene_decision_compute.handlers.handler_host_hygiene_decision import (
    decide_host_hygiene,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.dispatch_host_hygiene import (
    DispatchHostHygieneUnits,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.handler_host_hygiene_record_read import (
    HOST_DISK_READINGS,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.hold_reader import (
    InMemoryHostHoldReader,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_disk_readings import (
    HostDiskReadingStore,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.protocols import (
    ProtocolHostHoldReader,
    ProtocolHostHygieneLabWorkSender,
)

NODE_NAME = "node_host_hygiene_orchestrator"

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


class HostHygieneRunBase:
    """The body both host hygiene routes run: overlay in, decisions and units out."""

    HANDLER_ID: str

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        store: HostDiskReadingStore | None = None,
        sender: ProtocolHostHygieneLabWorkSender | None = None,
        hold_reader: ProtocolHostHoldReader | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelHostHygieneOrchestratorOverlay, environ=environ
        )
        self._store = HOST_DISK_READINGS if store is None else store
        self._sender = sender
        self._clock = clock
        self._hold_reader = (
            InMemoryHostHoldReader() if hold_reader is None else hold_reader
        )
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)
        self._dispatched: dict[tuple[str, str], dt.datetime] = {}

    def _output(
        self, correlation_id: uuid.UUID, events: tuple[ModelEventEnvelope[object], ...]
    ) -> ModelHandlerOutput[None]:
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id=self.HANDLER_ID,
            events=events,
        )

    def _overlay(self) -> ModelHostHygieneOrchestratorOverlay:
        overlay = self._gate.declaration()
        if not isinstance(overlay, ModelHostHygieneOrchestratorOverlay):
            raise NodeOverlayNotFoundError(
                f"{NODE_NAME}: the overlay is not a host hygiene orchestrator overlay"
            )
        return overlay

    def _held(
        self, overlay: ModelHostHygieneOrchestratorOverlay, now: dt.datetime
    ) -> Mapping[str, str]:
        try:
            return self._hold_reader.held_hosts(now, overlay.max_reading_age_seconds)
        except Exception as exc:
            # A hold that cannot be read is not assumed; the lab-work serve still
            # refuses a held host's unit.
            logging.getLogger(__name__).warning(
                "%s hold read failed (%s); holding nobody",
                type(self._hold_reader).__name__,
                type(exc).__name__,
            )
            return {}

    def _facts(
        self,
        overlay: ModelHostHygieneOrchestratorOverlay,
        held: Mapping[str, str],
    ) -> tuple[ModelHostHygieneHostFacts, ...]:
        return tuple(
            ModelHostHygieneHostFacts(
                host=row.name,
                cleanup_ids=row.cleanup_ids,
                held_reason=held.get(row.name),
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

    async def _run(
        self,
        *,
        overlay: ModelHostHygieneOrchestratorOverlay,
        now: dt.datetime,
        sender: ProtocolHostHygieneLabWorkSender,
        run_id: str,
        tick_id: str,
        trigger: EnumHostHygieneRunTrigger,
    ) -> ModelHostHygieneRunCompleted:
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
                hosts=self._facts(overlay, self._held(overlay, now)),
            )
        )
        # sender.send blocks until the host's receipt; the runtime calls handle on its loop, so run the dispatch off it.
        units = await asyncio.to_thread(
            DispatchHostHygieneUnits(overlay, sender).dispatch, decision.due_items()
        )
        for unit in units:
            if unit.outcome in _RAN:
                self._dispatched[(unit.host, unit.cleanup_id)] = now

        outcome = run_outcome(units)
        finished_at = self._clock()
        return ModelHostHygieneRunCompleted(
            run_id=run_id,
            job_id=overlay.job_id,
            tick_id=tick_id,
            started_at=now,
            finished_at=finished_at,
            outcome=outcome,
            decided=decision.items,
            units=units,
            trigger=trigger,
        )


__all__: list[str] = [
    "NODE_NAME",
    "HostHygieneRunBase",
]
