# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stopped-line fill's tick on the runtime's schedule (OMN-24029).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run
time; the tick that is due builds the fill handler on the profile the overlay names, runs one fill tick and returns
the run event. A refused overlay, an unbound profile, or a live profile whose store DSN variable is unset, is a failed
run event that names it, never a silent skip.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.floor_fill_scheduled_tick import (
    FloorFillProfileUnboundError,
    FloorFillStoreDsnUnsetError,
    run_floor_fill_tick,
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
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    ProtocolFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_scheduled_effect.models import (
    ModelFloorFillScheduleOverlay,
)

NODE_NAME = "node_floor_fill_scheduled_effect"


class HandlerFloorFillScheduled:
    """Gate on the schedule, build the fill on its profile, run one tick, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        build_handler: Callable[[Any], Any] | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        store: ProtocolFloorStateStore | None = None,
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelFloorFillScheduleOverlay, environ=environ
        )
        self._build_handler = build_handler
        self._environ = environ
        self._clock = clock
        self._store = store
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
            if not isinstance(overlay, ModelFloorFillScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a fill schedule overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        now = request.now.astimezone(UTC)
        if not self._gate.due(now):
            return None
        self._gate.mark(now)
        try:
            outcome = run_floor_fill_tick(
                fill_overlay_id=overlay.fill_overlay_id,
                profile=overlay.profile,
                environ=self._environ,
                build_handler=self._build_handler,
                store=self._store,
            )
        except NodeOverlayError:
            return self._event(
                request,
                "failed",
                f"the fill overlay {overlay.fill_overlay_id} could not be read",
            )
        except FloorFillStoreDsnUnsetError as exc:
            return self._event(
                request,
                "failed",
                f"UNKNOWN fill-state:store-dsn-unset:{exc.variable}",
            )
        except FloorFillProfileUnboundError:
            return self._event(
                request,
                "failed",
                f"the {overlay.profile} profile is not bound in the runtime yet",
            )
        return self._event(
            request,
            "completed" if outcome.exit_code == 0 else "failed",
            outcome.first_line,
        )


__all__: list[str] = ["HandlerFloorFillScheduled"]
