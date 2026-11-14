# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-ledger reducer's tick on the runtime's schedule (OMN-24305).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run
time; the tick that is due loads the schedule overlay and runs the work-ledger reducer through the shared tick module,
returning the run event. A refused overlay, an unbound read command or a reducer refusal is a failed run event that
names it, never a silent skip.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.handlers.work_ledger_reducer_scheduled_tick import (
    ModelWorkLedgerReducerTickOutcome,
    ProtocolWorkLedgerRowsReader,
    WorkLedgerReadError,
    build_rows_reader,
    run_work_ledger_reducer_tick,
)
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_work_ledger_reducer_scheduled_effect.models import (
    ModelWorkLedgerReducerScheduleOverlay,
)

NODE_NAME = "node_work_ledger_reducer_scheduled_effect"


class HandlerWorkLedgerReducerScheduled:
    """Gate on the schedule, run one work-ledger reducer run, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        tick: Callable[..., ModelWorkLedgerReducerTickOutcome] = (
            run_work_ledger_reducer_tick
        ),
        reader: ProtocolWorkLedgerRowsReader | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelWorkLedgerReducerScheduleOverlay, environ=environ
        )
        self._tick = tick
        self._reader = reader
        self._environ = environ
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)

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
            if not isinstance(overlay, ModelWorkLedgerReducerScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a work-ledger reducer schedule overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        now = request.now.astimezone(UTC)
        if not self._gate.due(now):
            return None
        self._gate.mark(now)
        try:
            reader = self._reader or build_rows_reader(overlay, self._environ)
            outcome = self._tick(overlay=overlay, reader=reader, clock=self._clock)
        except NodeOverlayError:
            return self._event(
                request,
                "failed",
                "the work-ledger reducer overlay could not be read",
            )
        except WorkLedgerReadError as exc:
            return self._event(request, "failed", str(exc))
        if outcome.failed:
            return self._event(request, "failed", outcome.detail)
        return self._event(request, "completed", outcome.detail)


__all__: list[str] = ["HandlerWorkLedgerReducerScheduled"]
