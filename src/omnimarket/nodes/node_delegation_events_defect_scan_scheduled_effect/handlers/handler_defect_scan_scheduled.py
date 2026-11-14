# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation_events defect scan's tick on the runtime's schedule (OMN-24271).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run
time; the tick that is due loads the schedule overlay and runs the defect scan's writing run through the shared tick
module, returning the run event. A refused overlay, an unreadable scan overlay or an unbound source is a failed run
event that names it, never a silent skip. A read of NOT WORKING is still a completed run: the scan ran and wrote its
row.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.defect_scan_scheduled_tick import (
    LedgerRouteError,
    ModelDefectScanTickOutcome,
    run_defect_scan_tick,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_delegation_events_defect_scan_scheduled_effect.models import (
    ModelDefectScanScheduleOverlay,
)

NODE_NAME = "node_delegation_events_defect_scan_scheduled_effect"


class HandlerDefectScanScheduled:
    """Gate on the schedule, run one writing scan, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        tick: Callable[..., ModelDefectScanTickOutcome] = run_defect_scan_tick,
        build: Callable[..., Any] | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelDefectScanScheduleOverlay, environ=environ
        )
        self._tick = tick
        self._build = build
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
            if not isinstance(overlay, ModelDefectScanScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a defect scan schedule overlay"
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
                scan_effect_overlay_id=overlay.scan_effect_overlay_id,
                scan_compute_overlay_id=overlay.scan_compute_overlay_id,
                environ=self._environ,
                clock=self._clock,
                build=self._build,
            )
        except NodeOverlayError as exc:
            return self._event(
                request,
                "failed",
                f"the defect scan overlay could not be read: {exc}",
            )
        except (ValueError, OSError, KeyError, LedgerRouteError):
            return self._event(
                request,
                "failed",
                "the defect scan sources are not bound in the runtime",
            )
        if outcome.failed:
            return self._event(request, "failed", outcome.detail)
        return self._event(request, "completed", outcome.detail)


__all__: list[str] = ["HandlerDefectScanScheduled"]
