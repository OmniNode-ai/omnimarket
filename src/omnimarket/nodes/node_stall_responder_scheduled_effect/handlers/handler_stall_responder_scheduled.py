# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stall responder on the runtime's schedule (OMN-25160).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run
time; the tick that is due loads the schedule overlay, reads the ledger window it names, asks the stall responder which
cause keys need a lane and returns the run event with the counts. The handler keeps the responder's dispatched map for
the life of the runtime process. A refused overlay or an unbound ledger route is a failed run event that names it,
never a silent skip.
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
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_stall_responder_scheduled_effect.handlers.stall_responder_tick import (
    LedgerRouteError,
    ModelStallResponderTickOutcome,
    run_stall_responder_tick,
)
from omnimarket.nodes.node_stall_responder_scheduled_effect.models import (
    ModelStallResponderScheduleOverlay,
)

NODE_NAME = "node_stall_responder_scheduled_effect"


class HandlerStallResponderScheduled:
    """Gate on the schedule, run one stall responder pass, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        tick: Callable[..., ModelStallResponderTickOutcome] = run_stall_responder_tick,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelStallResponderScheduleOverlay, environ=environ
        )
        self._tick = tick
        self._environ = environ
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)
        self._dispatched: Mapping[str, str] = {}

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
            if not isinstance(overlay, ModelStallResponderScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a stall responder schedule overlay"
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
                overlay=overlay,
                dispatched=self._dispatched,
                environ=self._environ,
                now=now,
            )
        except NodeOverlayError:
            return self._event(
                request, "failed", "the stall responder overlay could not be read"
            )
        except LedgerRouteError:
            return self._event(
                request,
                "failed",
                "the ledger route of the stall responder is not bound in the runtime",
            )
        if outcome.failed:
            return self._event(request, "failed", outcome.detail)
        self._dispatched = outcome.dispatched
        return self._event(request, "completed", outcome.detail)


__all__: list[str] = ["HandlerStallResponderScheduled"]
