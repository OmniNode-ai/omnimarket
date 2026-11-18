# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The commitment check runner's tick on the runtime's schedule (OMN-24029).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run
time; the tick that is due loads the schedule overlay and runs the commitment check runner through the shared tick
module, returning the run event. A refused overlay, an unreadable runner overlay or an unbound ledger source is a
failed run event that names it, never a silent skip.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.commitment_check_scheduled_tick import (
    LedgerRouteError,
    ModelCommitmentCheckTickOutcome,
    run_commitment_check_tick,
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
from omnimarket.nodes.node_commitment_check_scheduled_effect.models import (
    ModelCommitmentCheckScheduleOverlay,
)

NODE_NAME = "node_commitment_check_scheduled_effect"


class HandlerCommitmentCheckScheduled:
    """Gate on the schedule, run one commitment check run, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        tick: Callable[..., ModelCommitmentCheckTickOutcome] = (
            run_commitment_check_tick
        ),
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelCommitmentCheckScheduleOverlay, environ=environ
        )
        self._tick = tick
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
            if not isinstance(overlay, ModelCommitmentCheckScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a commitment check schedule overlay"
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
                check_overlay_id=overlay.check_overlay_id,
                environ=self._environ,
                clock=self._clock,
            )
        except NodeOverlayError:
            return self._event(
                request,
                "failed",
                "the commitment check overlay could not be read",
            )
        except LedgerRouteError:
            return self._event(
                request,
                "failed",
                "the commitment ledger source is not bound in the runtime",
            )
        if outcome.failed:
            return self._event(request, "failed", outcome.detail)
        return self._event(request, "completed", outcome.detail)


__all__: list[str] = ["HandlerCommitmentCheckScheduled"]
