# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dropped-work audit's tick on the runtime's schedule (OMN-26205).

Shape of HandlerCommitmentCheckScheduled: handle takes the runtime tick. A tick inside the overlay's interval returns
nothing and does not move the last-run time; the tick that is due runs the audit once through the shared tick module and
returns the run event. A refused overlay, an unreadable source or an unbound route is a failed run event that names it,
never a silent skip. The audit detects and raises; it assigns no owner and never admits or refuses work.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.dropped_work_audit_scheduled_tick import (
    DroppedWorkAuditSourceError,
    ModelDroppedWorkAuditTickOutcome,
    ProtocolOwnerMissingPublisher,
    run_dropped_work_audit_tick,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.dropped_work_audit import ModelDroppedWorkAuditScheduleOverlay
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_dropped_work_audit_scheduled_effect.adapters.publisher import (
    EmitOwnerMissingPublisher,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
)

NODE_NAME = "node_dropped_work_audit_scheduled_effect"


class HandlerDroppedWorkAuditScheduled:
    """Gate on the schedule, run one audit, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        tick: Callable[..., ModelDroppedWorkAuditTickOutcome] = (
            run_dropped_work_audit_tick
        ),
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        publisher: ProtocolOwnerMissingPublisher | None = None,
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME,
            model=ModelDroppedWorkAuditScheduleOverlay,
            environ=environ,
        )
        self._tick = tick
        self._environ = environ
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)
        self._evented: set[str] = set()
        self._publisher = publisher

    def _event(
        self,
        request: ModelRuntimeTick,
        outcome: Literal["completed", "failed"],
        detail: str,
    ) -> ModelScheduleRunEvent:
        return self._gate.run_event(
            tick_id=str(request.tick_id),
            scheduled_for=request.now,
            fired_at=self._clock(),
            outcome=outcome,
            detail=detail,
        )

    def handle(self, request: ModelRuntimeTick) -> ModelScheduleRunEvent | None:
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelDroppedWorkAuditScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a dropped-work audit schedule overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        now = request.now.astimezone(UTC)
        if not self._gate.due(now):
            return None
        self._gate.mark(now)
        try:
            outcome = self._tick(
                schedule=overlay,
                environ=self._environ,
                clock=self._clock,
                evented=self._evented,
                publisher=self._publisher
                if self._publisher is not None
                else EmitOwnerMissingPublisher(),
            )
        except NodeOverlayError:
            return self._event(
                request, "failed", "the dropped-work audit overlay could not be read"
            )
        except (LedgerRouteError, DroppedWorkAuditSourceError) as exc:
            return self._event(request, "failed", f"a source is not bound: {exc}")
        if outcome.failed:
            return self._event(request, "failed", outcome.detail)
        return self._event(request, "completed", outcome.detail)


__all__: list[str] = ["HandlerDroppedWorkAuditScheduled"]
