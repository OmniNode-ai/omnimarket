# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay drift read on the runtime's schedule (OMN-24271).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run
time; the tick that is due loads the schedule overlay and runs the overlay drift read through the shared tick module,
returning the run event. A refused overlay, an unreadable drift overlay or an unbound source is a failed run event
that names it, never a silent skip.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.handlers.overlay_drift_scheduled_tick import (
    LedgerRouteError,
    ModelOverlayDriftTickOutcome,
    run_overlay_drift_tick,
)
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_overlay_drift_scheduled_effect.models import (
    ModelOverlayDriftScheduleOverlay,
)

NODE_NAME = "node_overlay_drift_scheduled_effect"


class HandlerOverlayDriftScheduled:
    """Gate on the schedule, run one overlay drift read, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        tick: Callable[..., ModelOverlayDriftTickOutcome] = (run_overlay_drift_tick),
        build: Callable[..., Any] | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelOverlayDriftScheduleOverlay, environ=environ
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
            if not isinstance(overlay, ModelOverlayDriftScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not an overlay drift schedule overlay"
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
                drift_overlay_id=overlay.drift_overlay_id,
                environ=self._environ,
                clock=self._clock,
                build=self._build,
            )
        except NodeOverlayError:
            # NodeOverlayError is a ValueError, so it is caught first: an overlay
            # that cannot be read is not an unbound source.
            return self._event(
                request,
                "failed",
                "the overlay drift overlay could not be read",
            )
        except (ValueError, OSError, LedgerRouteError):
            return self._event(
                request,
                "failed",
                "the overlay drift sources are not bound in the runtime",
            )
        return self._event(
            request,
            "failed" if outcome.failed else "completed",
            outcome.detail,
        )


__all__: list[str] = ["HandlerOverlayDriftScheduled"]
