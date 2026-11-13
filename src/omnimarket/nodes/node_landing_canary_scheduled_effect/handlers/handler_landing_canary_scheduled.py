# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily landing canary on the runtime's schedule (OMN-24117).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the
last-run time; the tick that is due loads the schedule overlay, opens the canary's test pull request and
settles it, returning the run event. A refused overlay or an unreadable canary overlay is a failed run
event that names it, never a silent skip. One tick opens the canary and later ticks settle it, so a run
spans several ticks.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.landing_canary_scheduled_tick import (
    LedgerRouteError,
    ModelCanaryPendingRun,
    ModelLandingCanaryTickOutcome,
    NodeOverlayError,
    open_canary,
    settle_canary,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
)
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_landing_canary_scheduled_effect.models import (
    ModelLandingCanaryScheduleOverlay,
)

NODE_NAME = "node_landing_canary_scheduled_effect"


class HandlerLandingCanaryScheduled:
    """Gate on the schedule, open the canary's test pull request, settle it, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        open_run: Callable[..., ModelCanaryPendingRun] = open_canary,
        settle_run: Callable[..., ModelLandingCanaryTickOutcome | None] = settle_canary,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelLandingCanaryScheduleOverlay, environ=environ
        )
        self._open_run = open_run
        self._settle_run = settle_run
        self._environ = environ
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)
        self._run: ModelCanaryPendingRun | None = None

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
            if not isinstance(overlay, ModelLandingCanaryScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a landing canary schedule overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        now = request.now.astimezone(UTC)
        if self._run is None:
            if not self._gate.due(now):
                return None
            self._gate.mark(now)
            try:
                self._run = self._open_run(
                    canary_overlay_id=overlay.canary_overlay_id,
                    environ=self._environ,
                    clock=self._clock,
                )
            except NodeOverlayError:
                return self._event(
                    request,
                    "failed",
                    "the landing canary overlay could not be read",
                )
            except (LedgerRouteError, ValueError):
                return self._event(
                    request,
                    "failed",
                    "the landing canary source is not bound in the runtime",
                )
        try:
            outcome = self._settle_run(
                canary_overlay_id=overlay.canary_overlay_id,
                environ=self._environ,
                clock=self._clock,
                run=self._run,
            )
        except NodeOverlayError:
            self._run = None
            return self._event(
                request,
                "failed",
                "the landing canary overlay could not be read",
            )
        except (LedgerRouteError, ValueError):
            self._run = None
            return self._event(
                request,
                "failed",
                "the landing canary source is not bound in the runtime",
            )
        if outcome is None:
            return None
        self._run = None
        if outcome.failed:
            return self._event(request, "failed", outcome.detail)
        return self._event(request, "completed", outcome.detail)


__all__: list[str] = ["HandlerLandingCanaryScheduled"]
