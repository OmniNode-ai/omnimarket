# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The second benchmark arm's tick on the runtime's schedule (OMN-25052).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run
time; the tick that is due loads the schedule overlay, sends one delegate run pinned on the arm's backend id through
the shared tick module and returns the run event. A refused overlay is logged once; a run that no rung answered, that
another backend answered, or that printed no receipt is a failed run event that names it, never a silent skip.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.handlers.second_arm_scheduled_tick import (
    CommandRunner,
    run_command,
    run_second_arm_tick,
)
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_delegation_second_arm_scheduled_effect.models import (
    ModelSecondArmScheduleOverlay,
)

NODE_NAME = "node_delegation_second_arm_scheduled_effect"


class HandlerSecondArmScheduled:
    """Gate on the schedule, send one pinned delegate run, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        runner: CommandRunner = run_command,
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelSecondArmScheduleOverlay, environ=environ
        )
        self._clock = clock
        self._runner = runner
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
            if not isinstance(overlay, ModelSecondArmScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a second arm schedule overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        now = request.now.astimezone(UTC)
        if not self._gate.due(now):
            return None
        self._gate.mark(now)
        outcome = run_second_arm_tick(
            backend_id=overlay.backend_id,
            task_type=overlay.task_type,
            prompt=overlay.prompt,
            delegate_command=overlay.delegate_command,
            transport_args=overlay.transport_args,
            timeout_seconds=overlay.timeout_seconds,
            runner=self._runner,
        )
        return self._event(
            request, "failed" if outcome.failed else "completed", outcome.detail
        )


__all__: list[str] = ["HandlerSecondArmScheduled"]
