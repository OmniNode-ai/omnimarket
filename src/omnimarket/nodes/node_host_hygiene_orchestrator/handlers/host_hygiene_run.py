# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HostHygieneRun: the shared run body of the host hygiene orchestrator (OMN-24557).

Both the scheduled route and the on-demand route run the same body: read each
declared host's newest disk reading, ask the decision compute which cleanups are
due, send one lab work unit per due cleanup, remember the dispatched cleanups,
and build the run-completed result. The scheduled route keeps its gate, its
schedule run event and its envelopes; the on-demand route skips the gate and
returns the run event under the caller's correlation id.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable, Mapping

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
    EnumHostHygieneUnitOutcome,
    ModelHostHygieneOrchestratorOverlay,
    ModelHostHygieneRunCompleted,
    ModelHostHygieneUnitResult,
    run_outcome,
)
from omnimarket.nodes.node_host_hygiene_decision_compute.handlers.handler_host_hygiene_decision import (
    decide_host_hygiene,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.dispatch_host_hygiene import (
    DispatchHostHygieneUnits,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.handler_host_hygiene_record_read import (
    HOST_CAPACITY,
    HOST_DISK_READINGS,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_capacity_readings import (
    HostCapacityStore,
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


class DispatchMemory:
    """Remembers which cleanups ran, so a due tick does not re-send them early."""

    def __init__(self) -> None:
        self._dispatched: dict[tuple[str, str], dt.datetime] = {}

    def remember(self, host: str, cleanup_id: str, at: dt.datetime) -> None:
        self._dispatched[(host, cleanup_id)] = at

    def last(self, host: str, cleanup_id: str) -> dt.datetime | None:
        return self._dispatched.get((host, cleanup_id))

    def has(self, host: str, cleanup_id: str) -> bool:
        return (host, cleanup_id) in self._dispatched


#: Module-level default memory, shared by handlers that do not pass their own.
DISPATCH_MEMORY = DispatchMemory()


class HostHygieneRun:
    """The shared run body: facts, decision, dispatch, memory, and the run result."""

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
        self._store = HOST_DISK_READINGS if store is None else store
        self._capacity = HOST_CAPACITY if capacity is None else capacity
        self._sender = sender
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)
        self._memory = memory if memory is not None else DISPATCH_MEMORY

    def _facts(
        self, overlay: ModelHostHygieneOrchestratorOverlay, now: dt.datetime
    ) -> tuple[ModelHostHygieneHostFacts, ...]:
        return tuple(
            ModelHostHygieneHostFacts(
                host=row.name,
                cleanup_ids=row.cleanup_ids,
                reading=self._store.reading_for(row.name, row.pressure_volume),
                held_reason=self._capacity.held_reason_for(
                    row.name, now, overlay.max_reading_age_seconds
                ),
                last_dispatched=tuple(
                    ModelHostHygieneLastDispatch(
                        cleanup_id=cleanup_id,
                        dispatched_at=self._memory.last(row.name, cleanup_id),
                    )
                    for cleanup_id in row.cleanup_ids
                    if self._memory.has(row.name, cleanup_id)
                ),
            )
            for row in overlay.hosts
        )

    def declaration(self) -> ModelHostHygieneOrchestratorOverlay:
        """The overlay, or a refusal logged once; the caller decides the output."""
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelHostHygieneOrchestratorOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a host hygiene orchestrator overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            raise
        self._refusals.recovered()
        return overlay

    async def run(
        self,
        overlay: ModelHostHygieneOrchestratorOverlay,
        now: dt.datetime,
        run_id: str,
        tick_id: str,
    ) -> tuple[ModelHostHygieneRunCompleted, tuple[ModelHostHygieneUnitResult, ...]]:
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
                hosts=self._facts(overlay, now),
            )
        )
        sender = self._sender if self._sender is not None else build_sender(overlay)
        # sender.send blocks until the host's receipt; the runtime calls handle on its loop, so run the dispatch off it.
        units = await asyncio.to_thread(
            DispatchHostHygieneUnits(overlay, sender).dispatch, decision.due_items()
        )
        for unit in units:
            if unit.outcome in _RAN:
                self._memory.remember(unit.host, unit.cleanup_id, now)

        outcome = run_outcome(units)
        finished_at = self._clock()
        run = ModelHostHygieneRunCompleted(
            run_id=run_id,
            job_id=overlay.job_id,
            tick_id=tick_id,
            started_at=now,
            finished_at=finished_at,
            outcome=outcome,
            decided=decision.items,
            units=units,
        )
        return run, units


__all__: list[str] = [
    "DISPATCH_MEMORY",
    "DispatchMemory",
    "HostHygieneRun",
    "NODE_NAME",
]
