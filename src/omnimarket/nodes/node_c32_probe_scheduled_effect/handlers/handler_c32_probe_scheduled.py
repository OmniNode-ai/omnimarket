# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C32 auth-boundary probe's tick on the runtime's schedule (OMN-24271).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run
time; the tick that is due loads the schedule overlay and runs the C32 auth-boundary probe through the shared tick
module, returning the run event. A refused overlay, an unreadable probe overlay or a result topic the probe's
contract does not declare is a failed run event that names it, never a silent skip.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.c32_scheduled_tick import (
    ModelC32ProbeTickOutcome,
    run_c32_probe_tick,
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
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.protocols.protocol_c32_prober import (
    ProtocolC32Prober,
    ProtocolC32ResultPublisher,
)
from omnimarket.nodes.node_c32_probe_scheduled_effect.models import (
    ModelC32ProbeScheduleOverlay,
)

NODE_NAME = "node_c32_probe_scheduled_effect"


class HandlerC32ProbeScheduled:
    """Gate on the schedule, run one C32 auth-boundary probe run, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        tick: Callable[..., ModelC32ProbeTickOutcome] = (run_c32_probe_tick),
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        prober: ProtocolC32Prober | None = None,
        publisher: ProtocolC32ResultPublisher | None = None,
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelC32ProbeScheduleOverlay, environ=environ
        )
        self._tick = tick
        self._environ = environ
        self._clock = clock
        self._prober = prober
        self._publisher = publisher
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
            if not isinstance(overlay, ModelC32ProbeScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a C32 probe schedule overlay"
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
                probe_overlay_id=overlay.probe_overlay_id,
                environ=self._environ,
                clock=self._clock,
                prober=self._prober,
                publisher=self._publisher,
            )
        except (NodeOverlayError, ValueError) as exc:
            return self._event(
                request,
                "failed",
                f"the C32 probe overlay could not be read: {exc}",
            )
        if outcome.failed:
            return self._event(request, "failed", outcome.detail)
        return self._event(request, "completed", outcome.detail)


__all__: list[str] = ["HandlerC32ProbeScheduled"]
